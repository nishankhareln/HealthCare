"""
Backend-managed JWT authentication for the FastAPI layer.

Replaces the earlier Cognito-based design. The system has a `users`
table (id, username, password_hash) and issues its own JWTs at
`POST /auth/login`. Every protected endpoint depends on
`get_current_user` to verify the token.

`get_current_user` is the single FastAPI dependency every protected
endpoint attaches to. It:

  1. Extracts the Bearer token from the Authorization header.
  2. Honors a *narrowly-scoped* dev bypass (ENVIRONMENT='dev' AND the
     token is exactly `dev-test-token`) so local work doesn't require
     a real login.
  3. Verifies the JWT signature (HS256), issuer, expiration, and
     issued-at claims using `JWT_SECRET_KEY` from settings.
  4. Returns a frozen `AuthenticatedUser` carrying the user id and
     the username — the fields downstream handlers use for audit
     attribution. There are no roles or facilities (single-tenant).

`hash_password` and `verify_password` wrap bcrypt for the
login flow.

`issue_access_token` is what `POST /auth/login` calls after verifying
credentials.

Security posture:
  * Algorithm is pinned to HS256; `none` and any RSA/ECDSA variant
    are rejected. The single secret in `JWT_SECRET_KEY` is what signs
    every issued token.
  * All verification failures surface as HTTP 401 with the generic
    detail "Unauthorized" — error messages do not tell the client WHY
    verification failed, so crafted-input attackers get no oracle.
  * Internal logs carry only failure TYPE, never the token itself,
    never full claim dicts, never the raw password.
  * Tokens are size-capped at 8 KiB before any parse, so oversized
    input cannot pin memory in the JOSE parser.
  * Passwords are bcrypt-hashed (cost factor 12 by default). The
    plain password is never logged, never stored, never returned.
  * The dev-bypass token is compared with `secrets.compare_digest` to
    avoid timing side channels.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import jwt
from jose.exceptions import (
    ExpiredSignatureError,
    JWKError,
    JWTClaimsError,
    JWTError,
)
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict

from config import Settings, get_settings


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Security policy constants
# --------------------------------------------------------------------------- #
# HS256 = HMAC-SHA256. Pinning the allow-list defeats the classic
# "alg: none" and asymmetric-via-symmetric confusion attacks.
_ALLOWED_ALGORITHMS: tuple[str, ...] = ("HS256",)

# Hard cap so a megabyte of "token" cannot reach the JOSE parser.
_MAX_TOKEN_SIZE = 8192

# Clock skew tolerance.
_CLOCK_SKEW_SECONDS = 30

# Dev-only bypass. Only honored when ENVIRONMENT='dev'.
_DEV_BYPASS_TOKEN = "dev-test-token"  # noqa: S105 — intentional constant
_DEV_USER_ID = "dev-00000000-0000-4000-8000-000000000000"
_DEV_USERNAME = "dev"


# --------------------------------------------------------------------------- #
# Output model
# --------------------------------------------------------------------------- #
class AuthenticatedUser(BaseModel):
    """
    The authenticated caller, as resolved from a verified JWT (or dev bypass).

    Single-tenant deployment: no role, no facility. Just identity.

    `frozen=True` so handlers can't mutate the object mid-request and
    accidentally re-use a different identity.
    """

    id: str
    username: str

    # Compatibility shims so existing handlers that read `email`, `role`,
    # or `facility_id` keep working without touching every call site.
    # These are derived from the username/identity; they are NOT separate
    # fields the user can change.
    @property
    def email(self) -> Optional[str]:
        return None

    @property
    def role(self) -> str:
        return "caregiver"

    @property
    def facility_id(self) -> Optional[str]:
        return None

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
# Cost 12 = ~250 ms per hash on modern hardware. Slow enough to make
# brute-force attacks expensive, fast enough that a legitimate login
# does not feel sluggish.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """
    Bcrypt-hash a password. The result is what gets stored in
    `users.password_hash`. Same plain password produces a different
    hash every call (bcrypt uses a per-hash salt).
    """
    if not isinstance(plain, str) or not plain:
        raise ValueError("password must be a non-empty string")
    if len(plain) > 1024:
        # Bcrypt's own limit is 72 bytes; we cap further upstream so
        # an attacker can't burn CPU by submitting a megabyte of "password".
        raise ValueError("password is too long")
    return _pwd_context.hash(plain)


def verify_password(plain: str, password_hash: str) -> bool:
    """
    Constant-time check of `plain` against a stored bcrypt hash.
    Returns False on any error rather than raising.
    """
    if not isinstance(plain, str) or not plain:
        return False
    if not isinstance(password_hash, str) or not password_hash:
        return False
    try:
        return _pwd_context.verify(plain, password_hash)
    except Exception:  # noqa: BLE001 — passlib can raise on malformed hash
        return False


# --------------------------------------------------------------------------- #
# JWT issuance
# --------------------------------------------------------------------------- #
def issue_access_token(
    *,
    user_id: str,
    username: str,
    settings: Optional[Settings] = None,
) -> tuple[str, int]:
    """
    Sign and return a fresh access token for `user_id` + `username`.

    Returns `(token, expires_in_seconds)`. The expiry is read from
    settings so ops can tune it without code changes.

    The token's claims are intentionally minimal:
      * sub  — user id (UUID string)
      * username — display name (no PII beyond the username)
      * iss  — issuer identifier (settings.JWT_ISSUER)
      * iat  — issued at (epoch seconds)
      * exp  — expires at (epoch seconds)

    No `role`, no `facility_id`, no `email`, no `aud` — single tenant.
    """
    settings = settings or get_settings()
    if not settings.JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured")

    expires_in = settings.JWT_EXPIRY_MINUTES * 60
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(
        claims,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return token, expires_in


# --------------------------------------------------------------------------- #
# Bearer scheme (OpenAPI gets the security scheme documented automatically)
# --------------------------------------------------------------------------- #
# auto_error=False → we raise our own uniform 401s instead of letting
# fastapi.security raise a 403 with a different shape.
bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")


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
    return AuthenticatedUser(id=_DEV_USER_ID, username=_DEV_USERNAME)


def _extract_claim_str(claims: dict[str, Any], key: str, max_len: int) -> Optional[str]:
    """Return a string claim only if present, typed, and reasonably short."""
    value = claims.get(key)
    if not isinstance(value, str):
        return None
    if not value or len(value) > max_len:
        return None
    return value


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

    # Hard size cap BEFORE any parsing.
    if len(token) > _MAX_TOKEN_SIZE:
        logger.info("auth: token exceeded max size")
        raise _unauthorized()

    # ------------------------------------------------------------------ #
    # Narrow, explicit dev bypass
    # ------------------------------------------------------------------ #
    if settings.is_dev and secrets.compare_digest(token, _DEV_BYPASS_TOKEN):
        logger.debug("auth: dev bypass used")
        return _build_dev_user()

    # ------------------------------------------------------------------ #
    # Header sanity check (parse without verification first)
    # ------------------------------------------------------------------ #
    try:
        header = jwt.get_unverified_header(token)
    except (JWTError, JWKError):
        logger.info("auth: could not parse JWT header")
        raise _unauthorized() from None

    alg = header.get("alg")
    if alg not in _ALLOWED_ALGORITHMS:
        logger.info("auth: rejected token with alg=%s", alg)
        raise _unauthorized()

    if not settings.JWT_SECRET_KEY:
        # Mis-configured server: refuse rather than silently accept any token.
        logger.error("auth: JWT_SECRET_KEY is not configured")
        raise _unauthorized()

    # ------------------------------------------------------------------ #
    # Full cryptographic + claims verification
    # ------------------------------------------------------------------ #
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=list(_ALLOWED_ALGORITHMS),
            issuer=settings.JWT_ISSUER,
            options={
                "verify_signature": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
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

    # ------------------------------------------------------------------ #
    # Extract minimal fields; drop the raw claim dict on the floor
    # ------------------------------------------------------------------ #
    sub = _extract_claim_str(claims, "sub", max_len=128)
    username = _extract_claim_str(claims, "username", max_len=64)
    if sub is None or username is None:
        logger.info("auth: required claim missing")
        raise _unauthorized()

    # Sanity floor on iat (defense in depth — jose already verifies).
    iat_raw = claims.get("iat")
    if not isinstance(iat_raw, (int, float)) or iat_raw <= 0 or iat_raw > time.time() + _CLOCK_SKEW_SECONDS:
        logger.info("auth: iat claim invalid")
        raise _unauthorized()

    return AuthenticatedUser(id=sub, username=username)


# --------------------------------------------------------------------------- #
# Lifespan compatibility — no longer holds an HTTP client (no JWKS)
# --------------------------------------------------------------------------- #
async def close_http_client() -> None:
    """
    Compat no-op. The previous implementation held an httpx client for
    Cognito JWKS fetching; we no longer need one. main.py's lifespan
    still calls this on shutdown — keeping the symbol avoids editing
    the lifespan and any tests that mock it.
    """
    return None


# --------------------------------------------------------------------------- #
# Optional role-gate factory (single tenant: everyone is "caregiver")
# --------------------------------------------------------------------------- #
def require_role(*allowed_roles: str):
    """
    Compat shim — the old design supported per-role gates. Single-tenant
    deployments only have one role today, so this allows any
    authenticated user. Future role expansion should reintroduce the
    real role check by adding a `role` column to `users` and threading
    it into `AuthenticatedUser`.
    """
    if not allowed_roles:
        raise ValueError("require_role needs at least one allowed role")

    async def _dep(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        return user

    return _dep


# --------------------------------------------------------------------------- #
# Test hooks
# --------------------------------------------------------------------------- #
def _reset_state_for_tests() -> None:
    """Compat no-op — there is no module-level cache to reset anymore."""
    return None


__all__ = [
    "AuthenticatedUser",
    "bearer_scheme",
    "close_http_client",
    "get_current_user",
    "hash_password",
    "issue_access_token",
    "require_role",
    "verify_password",
]
