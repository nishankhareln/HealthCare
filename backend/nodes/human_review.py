"""
Human review node.

If there are no flagged updates, this node is a no-op. If there are, it
suspends the graph via LangGraph's `interrupt(...)` and waits for the
caregiver's decisions to arrive from POST /review/{patient_id}.

Position in the REASSESSMENT graph:
    confidence → HUMAN_REVIEW → merge → ...

Inputs (from state):
    run_id, patient_id
    flagged_updates: list[dict]  — from confidence_node; each item has
                                   field_path / new_value / source_phrase /
                                   reasoning / confidence / _flag_reason

Outputs (merged into state):
    approved_updates: list[dict] — the set the merge node will apply,
                                   drawn from flagged_updates based on
                                   the caregiver's per-item action.
    human_decisions:  list[dict] — raw record of what the reviewer
                                   submitted; preserved on state for
                                   audit transparency (the audit node
                                   writes a row per APPLIED update; this
                                   list captures the reviewer's intent
                                   including reject/edit/approve).

Security posture:
  * The reviewer's submission is validated at the API boundary through
    `models.ReviewSubmission` before it is passed to `Command(resume=...)`.
    This node re-validates each decision through `ReviewDecision` as a
    defence-in-depth check — the graph's resume path is a trust boundary
    and we refuse malformed data.
  * Decisions are matched against flagged_updates by a stable
    `update_id` that this node assigns BEFORE interrupting. The UI shows
    the same id back to the caregiver; the server uses it to match.
    Matching by list index is refused (UIs that reorder the list
    client-side would corrupt the mapping).
  * `approve` → copy the flagged update into approved_updates verbatim.
  * `edit`    → copy it, override `new_value` with the size-bounded
                 edited value, force `confidence=1.0` (a caregiver's
                 edited value is by definition authoritative for the
                 merge — confidence becomes about "is this mapped to a
                 real field", which a caregiver edit does not
                 re-validate; see note below).
  * `reject`  → the update is DROPPED from approved_updates. A record
                 is still written to `human_decisions` so the audit
                 trail can see that a caregiver actively rejected it.
  * A decision referencing an `update_id` that is not in flagged_updates
    is refused (ValueError). This prevents an attacker-forged decision
    from writing to a field the mapper never proposed.
  * If the reviewer submits fewer decisions than flagged items (missing
    any id), we refuse rather than guess — a silent default would mean
    a caregiver's incomplete submission gets merged as if they had
    approved the rest.
  * If `action == "edit"` but `edited_value is None`, we refuse. The
    API validator already checks this, but repeating it here means a
    bad Command(resume=...) from somewhere internal still cannot sneak
    through.
  * We NEVER short-circuit the interrupt when flagged_updates is non-
    empty. Silently skipping human review for any reason would be the
    single most dangerous failure this system can have — a clinician's
    statement reaches the record unreviewed.
  * No log of field_path, new_value, edited_value, or source_phrase.
    Counts and run_id only.

Note on `interrupt`:
    LangGraph's `interrupt()` suspends the graph and, when resumed with
    `Command(resume=value)`, returns `value` at the call site. We pass
    our flagged list (with injected ids) as the payload for the UI to
    render, and expect back a dict like {"decisions": [...]}.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from langgraph.types import interrupt
from pydantic import ValidationError

from models import MAX_DECISIONS_PER_SUBMISSION, ReviewDecision
from state import PipelineState
from utils import utc_now_iso

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Stable update-id generation
# --------------------------------------------------------------------------- #
def _make_update_id(run_id: str, index: int, flagged: dict[str, Any]) -> str:
    """
    Deterministic id for a flagged update.

    Format: `{index:04d}-{sha16}` where sha16 is the first 16 hex chars
    of sha256 over (run_id | index | field_path | str(new_value)). The
    index prefix gives a human-scannable ordering; the hash makes
    collisions impossible across runs.
    """
    field_path = str(flagged.get("field_path", ""))
    new_value_repr = repr(flagged.get("new_value"))
    material = f"{run_id}|{index}|{field_path}|{new_value_repr}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:16]
    return f"{index:04d}-{digest}"


def _annotate_flagged_for_review(
    *,
    run_id: str,
    flagged: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """
    Inject a stable `update_id` into each flagged item and return both
    the annotated list (for the reviewer UI) and an index keyed by id
    (for matching decisions back).
    """
    annotated: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for idx, item in enumerate(flagged):
        if not isinstance(item, dict):
            continue
        update_id = _make_update_id(run_id, idx, item)
        copy = dict(item)
        copy["update_id"] = update_id
        annotated.append(copy)
        by_id[update_id] = copy
    return annotated, by_id


# --------------------------------------------------------------------------- #
# Decision processing
# --------------------------------------------------------------------------- #
def _coerce_decisions(raw: Any) -> list[ReviewDecision]:
    """
    Pull the decisions list out of the resume payload and re-validate
    each one through the Pydantic model.

    Accepts either:
      * a list of decision dicts, or
      * a dict with a `decisions` key whose value is that list.
    """
    if isinstance(raw, dict):
        raw_list = raw.get("decisions")
    elif isinstance(raw, list):
        raw_list = raw
    else:
        raise ValueError("review resume payload must be a dict or list")

    if not isinstance(raw_list, list):
        raise ValueError("review decisions must be a list")
    if not raw_list:
        raise ValueError("review decisions list is empty")
    if len(raw_list) > MAX_DECISIONS_PER_SUBMISSION:
        raise ValueError(
            f"review decisions list exceeds cap of {MAX_DECISIONS_PER_SUBMISSION}"
        )

    decisions: list[ReviewDecision] = []
    for item in raw_list:
        if not isinstance(item, dict):
            raise ValueError("review decision must be an object")
        try:
            decisions.append(ReviewDecision.model_validate(item))
        except ValidationError as exc:
            # Don't echo the item contents — PHI risk — only the error
            # class.
            raise ValueError(
                f"review decision failed validation: {type(exc).__name__}"
            ) from exc
    return decisions


def _apply_decisions(
    *,
    decisions: list[ReviewDecision],
    by_id: dict[str, dict[str, Any]],
    run_id: str,
    user_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """
    Walk the decisions and produce (approved_updates, human_decisions,
    counts). Refuses submissions that don't cover every flagged id or
    that reference unknown ids.
    """
    seen_ids: set[str] = set()
    approved: list[dict[str, Any]] = []
    decisions_record: list[dict[str, Any]] = []
    counts = {"approved": 0, "edited": 0, "rejected": 0}

    for decision in decisions:
        if decision.update_id in seen_ids:
            raise ValueError("duplicate update_id in decisions")
        seen_ids.add(decision.update_id)

        flagged = by_id.get(decision.update_id)
        if flagged is None:
            # Unknown id — refuse rather than silently skip, because a
            # skipped decision could hide a tampering attempt.
            raise ValueError("decision references unknown update_id")

        base_record = {
            "update_id": decision.update_id,
            "action": decision.action,
            "field_path": flagged.get("field_path"),
            "user_id": user_id,
            "decided_at": utc_now_iso(),
            "run_id": run_id,
        }

        if decision.action == "approve":
            approved_entry = {
                "field_path": flagged.get("field_path"),
                "new_value": flagged.get("new_value"),
                "source_phrase": flagged.get("source_phrase"),
                "reasoning": flagged.get("reasoning"),
                # Confidence after caregiver approval: elevate to 1.0
                # for audit clarity — a human signed off.
                "confidence": 1.0,
            }
            approved.append(approved_entry)
            decisions_record.append({**base_record, "edited_value": None})
            counts["approved"] += 1

        elif decision.action == "edit":
            if decision.edited_value is None:
                raise ValueError(
                    "edit decision requires edited_value"
                )
            approved_entry = {
                "field_path": flagged.get("field_path"),
                "new_value": decision.edited_value,
                "source_phrase": flagged.get("source_phrase"),
                "reasoning": flagged.get("reasoning"),
                "confidence": 1.0,
            }
            approved.append(approved_entry)
            decisions_record.append(
                {**base_record, "edited_value": decision.edited_value}
            )
            counts["edited"] += 1

        elif decision.action == "reject":
            # No entry in `approved` — the update is dropped. Keep a
            # record of the rejection for audit visibility.
            decisions_record.append({**base_record, "edited_value": None})
            counts["rejected"] += 1

        else:
            # Pydantic's Literal should prevent this, but we guard
            # against future enum changes.
            raise ValueError("decision action is not recognised")

    missing = set(by_id.keys()) - seen_ids
    if missing:
        # Every flagged item must be decided on. Partial submissions
        # leave clinician statements in limbo; refuse rather than
        # default-approve or default-reject.
        raise ValueError(
            f"review submission missing {len(missing)} decisions"
        )

    return approved, decisions_record, counts


# --------------------------------------------------------------------------- #
# Node entry point
# --------------------------------------------------------------------------- #
def human_review_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: pause for caregiver review if needed.

    Synchronous — LangGraph's `interrupt` is a sync call that raises
    `GraphInterrupt` internally. The event loop is handled by the graph
    runtime; this function must NOT be `async def` because interrupt
    semantics differ between the two.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    user_id = state.get("user_id")
    flagged = state.get("flagged_updates") or []

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("human_review requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("human_review requires patient_id on state")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("human_review requires user_id on state")
    if not isinstance(flagged, list):
        raise TypeError("flagged_updates must be a list")

    if not flagged:
        # No flagged items — nothing to review.
        logger.info(
            "human_review skipped (no flagged items): run_id=%s patient_id=%s",
            run_id,
            patient_id,
        )
        return {
            "approved_updates": [],
            "human_decisions": [],
        }

    if len(flagged) > MAX_DECISIONS_PER_SUBMISSION:
        # The reviewer UI would also cap, but refuse at the source so a
        # malformed upstream cannot produce a submission the reviewer
        # cannot render.
        raise ValueError(
            f"flagged_updates exceeds cap of {MAX_DECISIONS_PER_SUBMISSION}"
        )

    annotated, by_id = _annotate_flagged_for_review(
        run_id=run_id, flagged=flagged
    )

    logger.info(
        "human_review interrupt: run_id=%s patient_id=%s flagged=%d",
        run_id,
        patient_id,
        len(annotated),
    )

    # Suspend the graph. `interrupt` returns only when
    # Command(resume=...) fires from the /review API endpoint.
    resume_payload = interrupt(
        {
            "run_id": run_id,
            "patient_id": patient_id,
            "flagged_updates": annotated,
        }
    )

    # On resume, validate and apply.
    decisions = _coerce_decisions(resume_payload)
    approved, decisions_record, counts = _apply_decisions(
        decisions=decisions,
        by_id=by_id,
        run_id=run_id,
        user_id=user_id,
    )

    logger.info(
        "human_review resumed: run_id=%s patient_id=%s "
        "approved=%d edited=%d rejected=%d",
        run_id,
        patient_id,
        counts["approved"],
        counts["edited"],
        counts["rejected"],
    )

    return {
        "approved_updates": approved,
        "human_decisions": decisions_record,
    }


__all__ = ["human_review_node"]
