"""
Pure-Python helpers used across the backend.

Kept deliberately small and dependency-free. Every function here is called
on the pipeline hot path (per-update, per-LLM-call), so the implementations
avoid heavy imports, logging with PHI, and exception translation that
would obscure real bugs.

Security posture:
  * `get_nested` / `set_nested` traverse dict paths driven by LLM output.
    Paths are length- and depth-bounded, empty segments are rejected, and
    `__dunder__` segments are refused as a belt-and-suspenders measure
    against future callers that might use `getattr` on a segment.
  * `set_nested` never overwrites an existing non-dict intermediate with
    a dict — doing so would silently destroy data. It raises `TypeError`
    instead so the caller (merge.py) can decide what to do.
  * `safe_json_parse` caps input size before invoking `re`, eliminating
    catastrophic-backtracking concerns with the simple `\\[.*\\]` fallback
    patterns.
  * `generate_run_id` uses `uuid.uuid4()`, which draws from `os.urandom`
    and is therefore cryptographically strong. We use it as an external
    identifier, so its unguessability matters.
  * No function here logs the dict, the path value, the text being
    parsed, or anything else that could contain PHI.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Final, Optional, Union


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# Matches FIELD_PATH_MAX_LEN in models.py. Duplicated rather than imported to
# keep utils.py free of circular-import risk.
_MAX_PATH_LENGTH: Final[int] = 255

# 20 is well above the depth of realistic assessment JSON (care_sections →
# mobility → in_room ≈ 3 levels) and prevents pathological stack use.
_MAX_PATH_DEPTH: Final[int] = 20

# 1 MiB is ~60x the largest legitimate Claude response at max_tokens=4096.
# Anything larger is discarded rather than regex-searched.
_MAX_JSON_PARSE_SIZE: Final[int] = 1 * 1024 * 1024

# Sentinel used to distinguish "caller did not pass a default" from
# "caller passed None as the default" in get_nested.
_MISSING: Final[object] = object()


# --------------------------------------------------------------------------- #
# Path handling
# --------------------------------------------------------------------------- #
def _split_path(path: str) -> list[str]:
    """
    Split a dotted path into segments and validate it.

    Raises `ValueError` on an empty, over-long, over-deep, or otherwise
    malformed path. The error message is generic — it does NOT echo the
    path value, to avoid leaking PHI-adjacent strings into logs if the
    exception is ever caught-and-logged carelessly.
    """
    if not isinstance(path, str):
        raise TypeError("path must be a string")
    if not path:
        raise ValueError("path must not be empty")
    if len(path) > _MAX_PATH_LENGTH:
        raise ValueError(f"path exceeds {_MAX_PATH_LENGTH} characters")

    segments = path.split(".")
    if len(segments) > _MAX_PATH_DEPTH:
        raise ValueError(f"path depth exceeds {_MAX_PATH_DEPTH} segments")

    for seg in segments:
        if not seg:
            raise ValueError("path contains an empty segment")
        if seg.startswith("__") and seg.endswith("__"):
            # A pure-dict traversal can't reach Python internals via a key
            # lookup, but forbidding dunder segments protects any future
            # caller that might `getattr` on a segment name.
            raise ValueError("path contains a dunder segment")

    return segments


def get_nested(
    d: dict[str, Any],
    path: str,
    default: Any = None,
) -> Any:
    """
    Return `d[a][b][c]...` for a dotted `path` like "a.b.c".

    Returns `default` (None by default) if:
      * any intermediate key is missing, or
      * an intermediate value is not a dict.

    Raises `ValueError`/`TypeError` ONLY for structurally-invalid paths
    (empty, over-long, dunder, non-string). Missing keys never raise.
    """
    if not isinstance(d, dict):
        raise TypeError("get_nested target must be a dict")
    segments = _split_path(path)

    current: Any = d
    for seg in segments:
        if not isinstance(current, dict):
            return default
        if seg not in current:
            return default
        current = current[seg]
    return current


def set_nested(
    d: dict[str, Any],
    path: str,
    value: Any,
) -> None:
    """
    Set `d[a][b][c]... = value` for a dotted `path` like "a.b.c".

    Intermediate dicts are created on demand. If an existing intermediate
    is a NON-dict (str, int, list, etc.), raises `TypeError` — the caller
    must decide whether to overwrite, and silent data loss is unsafe for a
    PHI system.

    Mutates `d` in place and returns None.
    """
    if not isinstance(d, dict):
        raise TypeError("set_nested target must be a dict")
    segments = _split_path(path)

    current = d
    for seg in segments[:-1]:
        existing = current.get(seg, _MISSING)
        if existing is _MISSING:
            new_dict: dict[str, Any] = {}
            current[seg] = new_dict
            current = new_dict
        elif isinstance(existing, dict):
            current = existing
        else:
            # Don't clobber a non-dict intermediate. The audit trail would
            # lose information about what used to live there.
            raise TypeError(
                f"cannot traverse into non-dict intermediate "
                f"(segment type={type(existing).__name__})"
            )

    current[segments[-1]] = value


# --------------------------------------------------------------------------- #
# Identifiers / timestamps
# --------------------------------------------------------------------------- #
def generate_run_id() -> str:
    """
    Return a fresh UUIDv4 as a lowercase 36-character string.

    `uuid.uuid4` draws from `os.urandom`, so the result is
    cryptographically strong and acceptable for use as an external
    identifier (path segment in S3, key in the DB, value on the wire).
    """
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    """
    Return the current UTC time as an ISO 8601 string with timezone suffix.

    Always timezone-aware; the result ends in `+00:00` rather than being a
    naive string that downstream consumers might misinterpret.
    """
    return datetime.now(timezone.utc).isoformat()


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware `datetime`."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# LLM output helpers
# --------------------------------------------------------------------------- #
def strip_json_fences(text: str) -> str:
    """
    Remove a leading Markdown code fence (```json or ```) and the matching
    trailing fence from `text`. Whitespace around the whole string is
    stripped as a convenience.

    Safe to call on text that does not contain fences — it returns the
    input (stripped of outer whitespace) unchanged.
    """
    if not isinstance(text, str):
        raise TypeError("strip_json_fences expects a string")

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    # Remove the opening fence line. The fence may be ```json, ```JSON,
    # ``` alone, or have arbitrary trailing chars before the newline.
    newline = stripped.find("\n")
    if newline == -1:
        # No newline — the whole thing is a single-line fenced token.
        body = stripped[3:]
    else:
        body = stripped[newline + 1:]

    body = body.rstrip()
    if body.endswith("```"):
        body = body[: -3]

    return body.strip()


def safe_json_parse(text: str) -> Optional[Union[list, dict]]:
    """
    Best-effort parse of LLM output to a `list` or `dict`.

    Strategy (fail closed on every step):
      1. Reject non-strings and oversize input immediately.
      2. Try `json.loads` on the raw text.
      3. Strip code fences and retry.
      4. Regex-extract the outermost `[...]` or `{...}` and retry.

    Returns the parsed value if it is a `list` or `dict`. Returns `None`
    if every strategy fails OR if the parsed value is a primitive (number,
    string, bool, null) — the caller asked for a structured value.

    Never raises. Never logs the input (it may contain PHI from the
    transcript that the LLM echoed back).
    """
    if not isinstance(text, str):
        return None
    if not text:
        return None
    if len(text) > _MAX_JSON_PARSE_SIZE:
        return None

    # Strategy 1: direct parse.
    parsed = _try_loads(text)
    if parsed is not None:
        return parsed

    # Strategy 2: strip fences.
    try:
        stripped = strip_json_fences(text)
    except TypeError:
        return None
    if stripped and stripped != text:
        parsed = _try_loads(stripped)
        if parsed is not None:
            return parsed
    else:
        stripped = text  # regex fallback operates on original

    # Strategy 3: regex-extract the outermost array or object.
    # Patterns are non-backtracking on fixed delimiters; input size is
    # bounded above, so catastrophic time is not possible.
    for pattern in (r"\[.*\]", r"\{.*\}"):
        match = re.search(pattern, stripped, re.DOTALL)
        if match is None:
            continue
        parsed = _try_loads(match.group(0))
        if parsed is not None:
            return parsed

    return None


def _try_loads(text: str) -> Optional[Union[list, dict]]:
    """Return a list/dict from `json.loads(text)` or None on any failure."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    if isinstance(value, (list, dict)):
        return value
    return None


__all__ = [
    "get_nested",
    "set_nested",
    "generate_run_id",
    "utc_now",
    "utc_now_iso",
    "strip_json_fences",
    "safe_json_parse",
]
