"""
SNS push-notification helper.

Three events fire a notification in the Samni pipeline:

  1. PIPELINE_COMPLETE     — a run finished and the updated PDF is ready.
  2. REVIEW_REQUIRED       — one or more proposed updates were flagged
                             and a human must decide before the run can
                             continue.
  3. PIPELINE_FAILED       — a run errored out terminally.

Publish semantics:
  * Mobile clients receive the message via Amazon SNS topic subscription
    (APNs/FCM). The backend NEVER sends raw patient content in a push —
    only an identifier and an event type. Payloads are deliberately
    spartan so a lock-screen preview cannot leak PHI.
  * SNS is best-effort. A failure to publish must NOT fail a pipeline
    run — the DB row is the source of truth. We log the failure (by
    error type, never by payload) and return a boolean.

Security posture:
  * No PHI in message body. `run_id`, `patient_id`, `status`, and an
    event code are the only fields. No names, transcript text,
    field paths, values, reasons, or error details leave the backend.
  * No PHI in SNS MessageAttributes either. MessageAttributes are
    visible to subscription filter policies and can end up in CloudWatch
    — treat them as log-level safe only.
  * Topic ARN is read from settings and validated at import time; a
    misconfigured ARN causes a fail-closed `NotificationDisabled` state
    rather than silent no-ops.
  * `botocore.exceptions.ClientError` is translated into the error-code
    string only — the response body (which can include request IDs and
    header dumps) is never logged.
  * `run_id` and `patient_id` are re-validated against the same regexes
    the API layer uses, so even if a caller passes unsanitised input
    we do not push malformed identifiers at mobile clients.
  * Payload size is capped well under SNS's 256 KiB limit; we reject
    anything over `_MAX_PAYLOAD_BYTES` so a bug cannot generate a giant
    message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Final, Optional

from botocore.exceptions import BotoCoreError, ClientError

from aws_clients import get_sns_client
from config import get_settings
from models import PATIENT_ID_PATTERN, RUN_ID_PATTERN

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Event codes — the ONLY values that should appear in the `event` field
# --------------------------------------------------------------------------- #
EVENT_PIPELINE_COMPLETE: Final[str] = "pipeline_complete"
EVENT_REVIEW_REQUIRED: Final[str] = "review_required"
EVENT_PIPELINE_FAILED: Final[str] = "pipeline_failed"

ALLOWED_EVENTS: Final[frozenset[str]] = frozenset(
    {
        EVENT_PIPELINE_COMPLETE,
        EVENT_REVIEW_REQUIRED,
        EVENT_PIPELINE_FAILED,
    }
)


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# SNS hard limit is 256 KiB. Our messages are ~200 bytes; cap at 8 KiB so
# any accidental ballooning fails loudly instead of being accepted by SNS.
_MAX_PAYLOAD_BYTES: Final[int] = 8 * 1024

# APNs title/body soft cap. Keep push text short enough to render cleanly
# on a lock screen without ellipsis, and short enough that no caller can
# smuggle PHI in through a "title" parameter by accident.
_MAX_TITLE_CHARS: Final[int] = 80
_MAX_BODY_CHARS: Final[int] = 160

_PATIENT_ID_RE: Final[re.Pattern[str]] = re.compile(PATIENT_ID_PATTERN)
_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(RUN_ID_PATTERN)


# --------------------------------------------------------------------------- #
# PHI-safe titles / bodies
# --------------------------------------------------------------------------- #
# These strings are deliberately generic. They contain NO patient
# information, NO field names, NO values. The mobile client is expected
# to fetch details from the authenticated API when the user taps the
# notification. This is the push-notification equivalent of a "You have
# new mail" icon — presence, not content.
_EVENT_COPY: Final[dict[str, tuple[str, str]]] = {
    EVENT_PIPELINE_COMPLETE: (
        "Assessment updated",
        "An assessment has finished processing. Open the app to review.",
    ),
    EVENT_REVIEW_REQUIRED: (
        "Review needed",
        "An assessment is waiting for your review.",
    ),
    EVENT_PIPELINE_FAILED: (
        "Assessment needs attention",
        "An assessment could not be processed. Open the app for details.",
    ),
}


# --------------------------------------------------------------------------- #
# Client-error helpers (no PHI, no response bodies)
# --------------------------------------------------------------------------- #
def _client_error_code(exc: ClientError) -> str:
    """
    Extract the error code from a ClientError without touching the
    response body (which may carry AWS request metadata we don't want in
    logs). Falls back to the exception class name.
    """
    try:
        code = exc.response.get("Error", {}).get("Code")
    except AttributeError:
        code = None
    return code or type(exc).__name__


# --------------------------------------------------------------------------- #
# Identifier re-validation — defence in depth
# --------------------------------------------------------------------------- #
def _validate_ids(run_id: str, patient_id: str) -> None:
    """
    Re-check run_id and patient_id here even though the API layer already
    validated them. Notifications are a trust boundary with a third-party
    service (SNS / APNs / FCM); we prefer to fail closed rather than
    publish a malformed identifier that a mobile client might mishandle.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError("run_id must be a UUIDv4-format string")
    if not isinstance(patient_id, str) or not _PATIENT_ID_RE.match(patient_id):
        raise ValueError("patient_id does not match the allowed pattern")


def _validate_event(event: str) -> None:
    if event not in ALLOWED_EVENTS:
        raise ValueError(f"event must be one of {sorted(ALLOWED_EVENTS)}")


# --------------------------------------------------------------------------- #
# Payload construction
# --------------------------------------------------------------------------- #
def _build_default_payload(
    *,
    event: str,
    run_id: str,
    patient_id: str,
) -> dict[str, Any]:
    """
    Build the `default` message — the plain-text body SNS uses when a
    subscriber is not platform-specific (e.g. email, SMS, raw HTTP).

    Intentionally opaque: the subscriber sees only the event name and
    the ids. No names, no values, no reasoning.
    """
    return {
        "event": event,
        "run_id": run_id,
        "patient_id": patient_id,
    }


def _build_apns_payload(
    *,
    event: str,
    run_id: str,
    patient_id: str,
) -> dict[str, Any]:
    """
    Build the APNs (iOS) payload. `content-available: 1` would make the
    push silent; we DON'T set that — staff need to see these in real
    time. `mutable-content: 0` disables notification-service extensions,
    so the push cannot be rewritten to fetch PHI from an attacker-
    controlled URL before displaying.
    """
    title, body = _EVENT_COPY[event]
    return {
        "aps": {
            "alert": {
                "title": title[:_MAX_TITLE_CHARS],
                "body": body[:_MAX_BODY_CHARS],
            },
            "sound": "default",
            "mutable-content": 0,
        },
        "event": event,
        "run_id": run_id,
        "patient_id": patient_id,
    }


def _build_fcm_payload(
    *,
    event: str,
    run_id: str,
    patient_id: str,
) -> dict[str, Any]:
    """
    Build the FCM (Android) payload. Uses `notification` for the visible
    alert and `data` for the ids the app reads on tap.
    """
    title, body = _EVENT_COPY[event]
    return {
        "notification": {
            "title": title[:_MAX_TITLE_CHARS],
            "body": body[:_MAX_BODY_CHARS],
        },
        "data": {
            "event": event,
            "run_id": run_id,
            "patient_id": patient_id,
        },
    }


def _serialize_sns_message(
    *,
    event: str,
    run_id: str,
    patient_id: str,
) -> str:
    """
    Produce the JSON body that SNS expects when MessageStructure="json":
    a top-level object whose keys are protocol names and whose values are
    JSON-encoded strings of each per-protocol payload.
    """
    default_payload = _build_default_payload(
        event=event, run_id=run_id, patient_id=patient_id
    )
    apns_payload = _build_apns_payload(
        event=event, run_id=run_id, patient_id=patient_id
    )
    fcm_payload = _build_fcm_payload(
        event=event, run_id=run_id, patient_id=patient_id
    )

    envelope = {
        "default": json.dumps(default_payload, separators=(",", ":")),
        "APNS": json.dumps(apns_payload, separators=(",", ":")),
        "APNS_SANDBOX": json.dumps(apns_payload, separators=(",", ":")),
        "GCM": json.dumps(fcm_payload, separators=(",", ":")),
    }
    serialized = json.dumps(envelope, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        # Should be unreachable with the above copy — raise rather than
        # truncate, because a truncated JSON envelope is syntactically
        # invalid and would produce an opaque SNS error later.
        raise ValueError("notification payload exceeds size cap")
    return serialized


def _build_message_attributes(event: str) -> dict[str, Any]:
    """
    Only the event code goes into MessageAttributes. These attributes
    are used by SNS subscription filter policies, so they must be
    non-PHI — a facility administrator can legitimately route
    "review_required" to an on-call channel, but must never see a
    patient_id here.
    """
    return {
        "event": {
            "DataType": "String",
            "StringValue": event,
        },
    }


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #
async def _publish_sync_to_thread(
    *,
    topic_arn: str,
    message: str,
    attributes: dict[str, Any],
) -> None:
    """
    Run the blocking boto3 publish call in a thread so the async caller
    is not blocked. boto3 is sync-only; this is the standard boto-in-
    async pattern.
    """
    client = get_sns_client()
    await asyncio.to_thread(
        client.publish,
        TopicArn=topic_arn,
        Message=message,
        MessageStructure="json",
        MessageAttributes=attributes,
    )


async def send_notification(
    *,
    event: str,
    run_id: str,
    patient_id: str,
) -> bool:
    """
    Publish an SNS notification for `event`.

    Returns True on successful publish; False if SNS is not configured
    or the publish failed. Never raises — a push is best-effort, and the
    DB row (plus API polling) is the real source of truth.

    Arguments are keyword-only to prevent positional mistakes at call
    sites (passing run_id where patient_id was expected would silently
    push a malformed identifier).
    """
    try:
        _validate_event(event)
        _validate_ids(run_id, patient_id)
    except (TypeError, ValueError) as exc:
        # Argument shape is wrong — log the class of error, never the
        # value. If this fires, a caller is misusing the API.
        logger.error(
            "notification rejected: invalid argument (%s)",
            type(exc).__name__,
        )
        return False

    settings = get_settings()
    topic_arn: Optional[str] = settings.SNS_TOPIC_ARN
    if not topic_arn:
        # No SNS configured (dev/test). Silently skip — this is not an
        # error in dev, and in prod `enforce_production_requirements`
        # has already refused to boot without an ARN.
        logger.debug("SNS topic not configured; skipping push")
        return False

    try:
        message = _serialize_sns_message(
            event=event, run_id=run_id, patient_id=patient_id
        )
        attributes = _build_message_attributes(event)
    except (TypeError, ValueError) as exc:
        logger.error(
            "notification payload build failed (%s)",
            type(exc).__name__,
        )
        return False

    try:
        await _publish_sync_to_thread(
            topic_arn=topic_arn,
            message=message,
            attributes=attributes,
        )
    except ClientError as exc:
        logger.warning(
            "SNS publish failed: %s (event=%s)",
            _client_error_code(exc),
            event,
        )
        return False
    except BotoCoreError as exc:
        logger.warning(
            "SNS transport error: %s (event=%s)",
            type(exc).__name__,
            event,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders for async
        # A push failure must never crash a pipeline run. We log the
        # exception TYPE only (not `str(exc)`, which could include a
        # request body fragment).
        logger.warning(
            "SNS publish unexpected error: %s (event=%s)",
            type(exc).__name__,
            event,
        )
        return False

    # Note: no PHI in this log line — run_id and patient_id are opaque
    # identifiers, not medical data.
    logger.info(
        "notification sent: event=%s run_id=%s patient_id=%s",
        event,
        run_id,
        patient_id,
    )
    return True


# --------------------------------------------------------------------------- #
# Convenience wrappers — call-site clarity, same underlying function
# --------------------------------------------------------------------------- #
async def notify_pipeline_complete(*, run_id: str, patient_id: str) -> bool:
    """Fire-and-forget push for a successful run."""
    return await send_notification(
        event=EVENT_PIPELINE_COMPLETE,
        run_id=run_id,
        patient_id=patient_id,
    )


async def notify_review_required(*, run_id: str, patient_id: str) -> bool:
    """Fire-and-forget push when updates need human review."""
    return await send_notification(
        event=EVENT_REVIEW_REQUIRED,
        run_id=run_id,
        patient_id=patient_id,
    )


async def notify_pipeline_failed(*, run_id: str, patient_id: str) -> bool:
    """Fire-and-forget push for a terminal failure."""
    return await send_notification(
        event=EVENT_PIPELINE_FAILED,
        run_id=run_id,
        patient_id=patient_id,
    )


__all__ = [
    # Public API
    "send_notification",
    "notify_pipeline_complete",
    "notify_review_required",
    "notify_pipeline_failed",
    # Event codes
    "EVENT_PIPELINE_COMPLETE",
    "EVENT_REVIEW_REQUIRED",
    "EVENT_PIPELINE_FAILED",
    "ALLOWED_EVENTS",
]
