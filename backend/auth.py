"""
Cognito JWT authentication for the FastAPI layer.

`get_current_user` is the single FastAPI dependency every protected endpoint
attaches to. It:

  1. Extracts the Bearer token from the Authorization header.
  2. Honors a *narrowly-scoped* dev bypass (ENVIRONMENT='dev' AND the
     token is exactly `dev-test-token`) so local work doesn't require a
     real Cognito pool.
  3. Downloads and caches Cognito's JWKS (TTLCache, refetched on kid miss
     to survive key rotation).
  4. Verifies the JWT signature (pinned to RS256), issuer, audience/client_id,
     expiration, and issued-at claims.
  5. Returns a frozen `AuthenticatedUser` carrying the sub claim,
     optional email, role (from cognito:groups), and facility_id (from
     a custom claim) — the fields downstream handlers use for audit
     attribution and tenant-scoped authorization.

Security posture:
  * Algorithm is pinned to RS256; `none` and any symmetric variant are
    rejected by jose because they aren't in the allow-list.
  * All verification failures surface as HTTP 401 with the generic
    detail "Unauthorized" — error messages do not tell the client WHY
    verification failed, so crafted-input attackers get no oracle.
  * Internal logs carry only failure TYPE (e.g. ExpiredSignatureError),
    never the token itself, never full claim dicts, never email values.
  * A concurrency lock around JWKS fetches prevents a thundering-herd
    of refetches when the cache expires.
  * The dev-bypass token is compared with `secrets.compare_digest` to
    avoid timing side channels (paranoid — the dev token isn't a secret
    — but costs nothing and removes one more "why not?" footgun).
  * JWT size is capped at 8 KiB before any parse, so oversized input
    can't pin memory in the JWKS/JOSE parsers.
  * JWKS is fetched over httpx with strict TLS verification (httpx
    default) and a short timeout; a stalled Cognito does not stall the
    auth path forever.
  * An AuthenticatedUser does NOT retain raw claims — those may contain
    PII (name, phone_number, email) that shouldn't ride along on the
    request scope where it might be logged by a lazy handler.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Optional

import httpx
from cachetools import TTLCache
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import (
    ExpiredSignatureError,
    JWKError,
    JWTClaimsError,
    JWTError,
)
from pydantic import BaseModel, ConfigDict

from config import Settings, get_settings


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Security policy constants
# --------------------------------------------------------------------------- #
# RS256 is what Cognito signs with. Pinning the allow-list defeats the
# classic "alg: none" and HS256-via-public-key confusion attacks.
_ALLOWED_ALGORITHMS: tuple[str, ...] = ("RS256",)

# Hard cap so a megabyte of "token" cannot reach the JOSE parser.
_MAX_TOKEN_SIZE = 8192

# Clock skew tolerance between Cognito's signer and our clock.
_CLOCK_SKEW_SECONDS = 30

# Dev-only bypass. Only honored when ENVIRONMENT='dev'.
_DEV_BYPASS_TOKEN = "dev-test-token"  # noqa: S105 — intentional constant
_DEV_USER_ID = "dev-00000000-0000-4000-8000-000000000000"
_DEV_USER_EMAIL = "dev@samnilabs.local"
_DEV_USER_FACILITY = "dev-facility"
_DEV_USER_ROLE = "caregiver"


# --------------------------------------------------------------------------- #
# Output model
# --------------------------------------------------------------------------- #
class AuthenticatedUser(BaseModel):
    """
    The authenticated caller, as resolved from a verified JWT (or dev bypass).

    Deliberately minimal: we only carry fields the app actually needs for
    audit attribution (`id`), display/notification (`email`),
    authorization (`role`, `facility_id`), and telemetry (`token_use`).
    Raw claims are discarded after verification.

    `frozen=True` so handlers can't mutate the object mid-request and
    accidentally re-use a different identity.
    """

    id: str
    email: Optional[str] = None
    role: str = "caregiver"
    facility_id: Optional[str] = None
    token_use: str

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Bearer scheme (OpenAPI gets the security scheme documented automatically)
# --------------------------------------------------------------------------- #
# auto_error=False → we raise our own uniform 401s instead of letting
# fastapi.security raise a 403 with a different shape.
bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")


# --------------------------------------------------------------------------- #
# JWKS cache + async HTTP client
# --------------------------------------------------------------------------- #
# We key the cache by URL so a future multi-pool deployment works transparently.
# TTL is read from settings on first use (see _get_jwks_cache).
_jwks_cache: Optional[TTLCache] = None
_jwks_lock = asyncio.Lock()
_http_client: Optional[httpx.AsyncClient] = None


def _get_jwks_cache() -> TTLCache:
    """Lazily build the TTLCache using the configured TTL."""
    global _jwks_cache
    if _jwks_cache is None:
        ttl = get_settings().COGNITO_JWKS_CACHE_TTL
        _jwks_cache = TTLCache(maxsize=4, ttl=ttl)
    return _jwks_cache


async def _get_http_client() -> httpx.AsyncClient:
    """Return a shared AsyncClient. Created lazily; closed in close_http_client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            # httpx verifies TLS by default; left explicit for reviewers.
            verify=True,
            follow_redirects=False,
        )
    return _http_client


async def close_http_client() -> None:
    """Close the shared AsyncClient. Called from the FastAPI shutdown hook."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        try:
            await _http_client.aclose()
        except Exception:  # noqa: BLE001 — shutdown path
            logger.exception("auth: error closing shared HTTP client")
    _http_client = None


# --------------------------------------------------------------------------- #
# JWKS fetching
# --------------------------------------------------------------------------- #
async def _fetch_jwks(settings: Settings, *, force: bool) -> dict[str, Any]:
    """
    Return the JWKS dict for the configured User Pool.

    The lock serializes concurrent misses so we don't fire N fetches when
    the cache expires under load. Under-lock re-check ensures a waiter
    benefits from a peer's fetch instead of duplicating it.
    """
    url = settings.cognito_jwks_url
    cache = _get_jwks_cache()

    async with _jwks_lock:
        if not force:
            cached = cache.get(url)
            if cached is not None:
                return cached

        client = await _get_http_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            logger.error(
                "auth: JWKS fetch failed err=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable",
            ) from None
        except ValueError:
            logger.error("auth: JWKS response was not JSON")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable",
            ) from None

        # Minimal structural sanity check — must be {"keys": [ ... ]}.
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("keys"), list)
            or not payload["keys"]
        ):
            logger.error("auth: JWKS payload malformed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable",
            )

        cache[url] = payload
        return payload


async def _find_signing_key(settings: Settings, kid: str) -> dict[str, Any]:
    """
    Return the JWK whose `kid` matches the token header.

    If the kid isn't in the cached JWKS, force ONE refetch to survive a
    Cognito key rotation, then give up.
    """
    jwks = await _fetch_jwks(settings, force=False)
    for key in jwks.get("keys", []):
        if isinstance(key, dict) and key.get("kid") == kid:
            return key

    # Unknown kid → invalidate and refetch once. Key rotation is the
    # legitimate cause; anything else is treated as auth failure.
    jwks = await _fetch_jwks(settings, force=True)
    for key in jwks.get("keys", []):
        if isinstance(key, dict) and key.get("kid") == kid:
            return key

    logger.info("auth: token kid not present in JWKS after refetch")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _unauthorized() -> HTTPException:
    """Uniform 401 — message never varies by reason (no oracle)."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _build_dev_user() -> AuthenticatedUser:
    """Synthetic user returned for the dev bypass."""
    return AuthenticatedUser(
        id=_DEV_USER_ID,
        email=_DEV_USER_EMAIL,
        role=_DEV_USER_ROLE,
        facility_id=_DEV_USER_FACILITY,
        token_use="id",
    )


def _extract_claim_str(claims: dict[str, Any], key: str, max_len: int) -> Optional[str]:
    """Return a string claim only if present, typed, and reasonably short."""
    value = claims.get(key)
    if not isinstance(value, str):
        return None
    if not value or len(value) > max_len:
        return None
    return value


def _extract_role(claims: dict[str, Any]) -> str:
    """
    Pick a role from `cognito:groups` if present, else fall back to 'caregiver'.

    Validates the group name is a bounded-length string — a malicious
    group name injected into the pool can't become a 10 MiB `role`
    attribute on the request.
    """
    groups = claims.get("cognito:groups")
    if isinstance(groups, list) and groups:
        first = groups[0]
        if isinstance(first, str) and 0 < len(first) <= 64 and first.isprintable():
            return first
    return "caregiver"


# --------------------------------------------------------------------------- #
# Public dependency
# --------------------------------------------------------------------------- #
async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """
    FastAPI dependency: verify the caller's Bearer JWT and return a user.

    Attach to any protected endpoint via `Depends(get_current_user)`.
    Raises 401 with a generic detail on any failure.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized()

    token = credentials.credentials

    # Hard size cap BEFORE any parsing. An 8 KiB ceiling is roughly 4x a
    # typical Cognito token with custom claims.
    if len(token) > _MAX_TOKEN_SIZE:
        logger.info("auth: token exceeded max size")
        raise _unauthorized()

    # ------------------------------------------------------------------ #
    # Narrow, explicit dev bypass
    # ------------------------------------------------------------------ #
    if settings.is_dev and secrets.compare_digest(token, _DEV_BYPASS_TOKEN):
        logger.debug("auth: dev bypass used")
        return _build_dev_user()

    # In non-dev, the bypass token is just another invalid JWT — it will
    # fail `get_unverified_header` below with a JWTError.

    # ------------------------------------------------------------------ #
    # Parse header → find signing key
    # ------------------------------------------------------------------ #
    try:
        header = jwt.get_unverified_header(token)
    except (JWTError, JWKError):
        logger.info("auth: could not parse JWT header")
        raise _unauthorized() from None

    alg = header.get("alg")
    kid = header.get("kid")
    if alg not in _ALLOWED_ALGORITHMS or not isinstance(kid, str) or not kid:
        logger.info("auth: rejected token with alg=%s kid_present=%s", alg, bool(kid))
        raise _unauthorized()

    signing_key = await _find_signing_key(settings, kid)

    # ------------------------------------------------------------------ #
    # Peek token_use to decide audience-verification policy
    # ------------------------------------------------------------------ #
    try:
        unverified_claims = jwt.get_unverified_claims(token)
    except (JWTError, JWKError):
        logger.info("auth: could not parse JWT claims")
        raise _unauthorized() from None

    token_use = unverified_claims.get("token_use")
    if token_use not in ("id", "access"):
        logger.info("auth: unknown token_use=%s", token_use)
        raise _unauthorized()

    # ID tokens carry `aud`; access tokens carry `client_id` instead.
    audience: Optional[str] = (
        settings.COGNITO_APP_CLIENT_ID if token_use == "id" else None
    )

    # ------------------------------------------------------------------ #
    # Full cryptographic + claims verification
    # ------------------------------------------------------------------ #
    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=list(_ALLOWED_ALGORITHMS),
            audience=audience,
            issuer=settings.cognito_issuer,
            options={
                "verify_signature": True,
                "verify_aud": token_use == "id",
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
                "require_aud": token_use == "id",
                "require_iat": True,
                "require_exp": True,
                "leeway": _CLOCK_SKEW_SECONDS,
            },
        )
    except ExpiredSignatureError:
        logger.info("auth: token expired")
        raise _unauthorized() from None
    except JWTClaimsError as exc:
        logger.info("auth: claim verification failed err=%s", type(exc).__name__)
        raise _unauthorized() from None
    except (JWTError, JWKError) as exc:
        logger.info("auth: signature verification failed err=%s", type(exc).__name__)
        raise _unauthorized() from None

    # Access tokens: python-jose did not verify `client_id` (non-standard).
    if token_use == "access":
        if claims.get("client_id") != settings.COGNITO_APP_CLIENT_ID:
            logger.info("auth: access token client_id mismatch")
            raise _unauthorized()

    # ------------------------------------------------------------------ #
    # Extract minimal fields; drop the raw claim dict on the floor
    # ------------------------------------------------------------------ #
    sub = _extract_claim_str(claims, "sub", max_len=128)
    if sub is None:
        logger.info("auth: sub claim missing or invalid")
        raise _unauthorized()

    email = _extract_claim_str(claims, "email", max_len=320)  # RFC 5321 cap
    role = _extract_role(claims)
    facility_id = _extract_claim_str(claims, "custom:facility_id", max_len=64)

    logger.debug(
        "auth: accepted token_use=%s role=%s facility_present=%s",
        token_use,
        role,
        facility_id is not None,
    )

    return AuthenticatedUser(
        id=sub,
        email=email,
        role=role,
        facility_id=facility_id,
        token_use=token_use,
    )


# --------------------------------------------------------------------------- #
# Optional role-gate factory (no-op unless used)
# --------------------------------------------------------------------------- #
def require_role(*allowed_roles: str):
    """
    Return a dependency that additionally requires the user's role be in
    `allowed_roles`. Future endpoints can use this for role-based access:

        @app.post("/admin/x")
        async def x(user = Depends(require_role("admin"))):
            ...
    """
    allowed = frozenset(allowed_roles)
    if not allowed:
        raise ValueError("require_role needs at least one allowed role")

    async def _dep(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if user.role not in allowed:
            logger.info("auth: role-gate denied role=%s", user.role)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden",
            )
        return user

    return _dep


# --------------------------------------------------------------------------- #
# Test hooks
# --------------------------------------------------------------------------- #
def _reset_state_for_tests() -> None:
    """Clear JWKS cache + client. Tests use this between fixtures."""
    global _jwks_cache, _http_client
    _jwks_cache = None
    _http_client = None


__all__ = [
    "AuthenticatedUser",
    "bearer_scheme",
    "get_current_user",
    "require_role",
    "close_http_client",
]
