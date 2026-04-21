"""
Confidence-routing node.

Takes `verified_updates` (output of the critic) and splits them into two
buckets based on a configurable threshold:

  * auto_approved   — confidence >= CONFIDENCE_THRESHOLD, will be merged
                      without human involvement.
  * flagged_updates — everything else, plus anything with an AMBIGUOUS
                      field_path or a malformed structure, will go to
                      the human_review interrupt.

Position in the graph:
    llm_critic → CONFIDENCE → (auto-approved + flagged) → human_review? → merge

Inputs (from state):
    verified_updates: list[dict]
    run_id, patient_id — identifiers for logging

Outputs (merged into state):
    auto_approved:    list[dict]
    flagged_updates:  list[dict]

Security posture:
  * Threshold is read from settings each call, NOT captured at import
    time. That way an ops change to CONFIDENCE_THRESHOLD takes effect on
    the next run instead of requiring a process restart.
  * Every item is re-validated through `models.UpdateObject` before it
    is placed in a bucket. Malformed items are routed to `flagged` so a
    human can see them, never silently dropped — quiet drops would mean
    a clinician's statement never reaches the record.
  * AMBIGUOUS is ALWAYS flagged regardless of confidence. The whole
    point of the sentinel is "I could not pick a field" — the LLM's
    stated confidence about a non-existent field is meaningless.
  * No new logic here that interprets the clinical content. We are a
    router, not a judge of meaning.
  * No field_path, source_phrase, or value is logged. Counts and
    run_id only.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from config import get_settings
from models import UpdateObject
from state import AMBIGUOUS_FIELD_PATH, PipelineState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# Mirrors the merge cap. The API already caps at 500 decisions per
# submission; a router receiving more than this means an earlier stage
# misbehaved.
_MAX_UPDATES_PER_ROUTING: int = 1000


def _as_update(item: Any) -> UpdateObject | None:
    """
    Re-validate a single update. Returns None if it cannot be revived;
    the caller will treat that as "flag for human review with a note",
    which is strictly safer than dropping it.
    """
    if isinstance(item, UpdateObject):
        return item
    if not isinstance(item, dict):
        return None
    try:
        return UpdateObject.model_validate(item)
    except ValidationError:
        return None


def _mark_flagged(
    raw: Any,
    reason: str,
) -> dict[str, Any]:
    """
    Produce a flagged-update dict. We copy the original shape when we
    can (so the human reviewer sees what the LLM actually produced) and
    add a private `_flag_reason` that the review UI can display.

    The reason field is limited to a short controlled vocabulary — it
    is safe to show operators and never echoes user content.
    """
    if isinstance(raw, dict):
        flagged = dict(raw)
    else:
        flagged = {"raw": None}  # malformed input — strip it
    flagged["_flag_reason"] = reason
    return flagged


def confidence_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: split `verified_updates` into auto-approved and
    flagged buckets using CONFIDENCE_THRESHOLD from settings.
    """
    run_id = state.get("run_id")
    verified_updates = state.get("verified_updates") or []

    if not isinstance(verified_updates, list):
        raise TypeError("verified_updates must be a list")

    total = len(verified_updates)
    if total > _MAX_UPDATES_PER_ROUTING:
        raise ValueError(
            f"confidence router received {total} updates, "
            f"exceeds cap of {_MAX_UPDATES_PER_ROUTING}"
        )

    settings = get_settings()
    threshold = settings.CONFIDENCE_THRESHOLD

    auto_approved: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    for raw in verified_updates:
        validated = _as_update(raw)
        if validated is None:
            flagged.append(_mark_flagged(raw, reason="validation_failed"))
            continue

        if validated.field_path == AMBIGUOUS_FIELD_PATH:
            flagged.append(
                _mark_flagged(
                    validated.model_dump(),
                    reason="ambiguous_field_path",
                )
            )
            continue

        # confidence is already bounded to [0.0, 1.0] by UpdateObject;
        # the comparison is safe.
        if validated.confidence >= threshold:
            auto_approved.append(validated.model_dump())
        else:
            flagged.append(
                _mark_flagged(
                    validated.model_dump(),
                    reason="below_threshold",
                )
            )

    logger.info(
        "confidence routing: run_id=%s total=%d auto=%d flagged=%d threshold=%.2f",
        run_id,
        total,
        len(auto_approved),
        len(flagged),
        threshold,
    )

    return {
        "auto_approved": auto_approved,
        "flagged_updates": flagged,
    }


__all__ = ["confidence_node"]
