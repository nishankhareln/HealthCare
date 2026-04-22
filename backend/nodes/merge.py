"""
Merge node.

Takes the approved updates (auto-approved + human-approved) and applies
them to a DEEP COPY of the master JSON, producing `final_json`. No other
node writes to the patient's master JSON on disk — this is the single
choke point where the new assessment document is assembled.

Position in the graph:
    ... → confidence → [human_review?] → MERGE → save_json → audit → generate_pdf

Inputs (from state):
    master_json:       dict  — the patient's current assessment document
    auto_approved:     list  — updates with confidence ≥ threshold
    approved_updates:  list  — updates the human approved (or edited);
                               absent on runs with zero flagged updates
    run_id / patient_id / user_id — identifiers for the audit trail

Outputs (merged into state):
    final_json:     dict  — new assessment document, ready for save_json
    audit_entries:  list  — one entry per applied update; consumed by
                             the audit node to persist to audit_trail
    (plus a `merged_count` / `skipped_count` pair on the audit meta
    so summarize_state can report it safely)

Security posture:
  * NO in-place mutation of state['master_json']. We build `final_json`
    from a deepcopy so a crash mid-merge cannot leave the input dict in
    a half-updated state and cannot corrupt the LangGraph checkpoint.
  * Each update is re-validated through `models.UpdateObject` BEFORE
    being applied. Earlier stages already validated, but the merge step
    is the last line of defence before PHI is written, and the cost of
    re-validation is trivial compared to the cost of corruption.
  * field_path="AMBIGUOUS" updates are NEVER applied — they must have
    been resolved to a concrete path by human review. If one slips
    through, it is recorded as `skipped` with reason="ambiguous_path"
    and the merge continues.
  * `equipment.*` list fields use APPEND semantics, so a caregiver
    adding a walker does not wipe the existing wheelchair. Append is
    triggered by the field type at the target path, NOT by anything
    the LLM said — we decide, not the model.
  * Every applied update produces an audit entry carrying the OLD and
    NEW value, the field_path, the source_phrase, the confidence, and
    the approval_method. This is PHI but belongs in the audit_trail
    table, which is append-only and already treated as PHI-sensitive.
  * All per-update failures are ISOLATED — a bad update is skipped with
    a recorded reason, the rest of the merge proceeds. We never abort
    the whole run because of one malformed update.
  * No function in this module logs field_path, old/new value, or
    source_phrase. Counts and run_id only.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from pydantic import ValidationError

from db_models import (
    APPROVAL_METHOD_AUTO,
    APPROVAL_METHOD_HUMAN,
)
from models import UpdateObject
from state import AMBIGUOUS_FIELD_PATH, PipelineState
from utils import get_nested, set_nested, utc_now_iso

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
# Upper bound on how many updates we will process in a single merge.
# Matches the API-layer ceiling; if we ever get more, something has gone
# wrong upstream (e.g. a runaway LLM response that slipped past earlier
# validation).
_MAX_UPDATES_PER_MERGE: int = 1000

# Top-level field paths whose VALUE is a list and that should receive
# append (not replace) semantics. The master JSON schema does not have
# many of these; keep the list small and explicit rather than inferring
# from runtime type — an attacker-controlled value could otherwise flip
# replace/append by lying about its type.
_APPEND_LIST_PREFIXES: tuple[str, ...] = (
    "equipment",
    "care_sections.equipment",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_append_path(field_path: str) -> bool:
    """
    Return True if `field_path` targets a list-append field per the
    schema-level allowlist above. Matches exact path OR any path that
    resolves under one of the allowlisted list prefixes.
    """
    if field_path in _APPEND_LIST_PREFIXES:
        return True
    for prefix in _APPEND_LIST_PREFIXES:
        if field_path == prefix or field_path.startswith(prefix + "."):
            return True
    return False


def _coerce_to_update_object(update: Any) -> Optional[UpdateObject]:
    """
    Re-validate `update` through the Pydantic contract.

    Returns None if the update cannot be revived — the caller will
    record a `skipped` audit entry. We deliberately do not raise here
    because partial success is safer than all-or-nothing for a run
    that might have a single bad update among 20 good ones.
    """
    if isinstance(update, UpdateObject):
        return update
    if not isinstance(update, dict):
        return None
    try:
        return UpdateObject.model_validate(update)
    except ValidationError:
        return None


def _make_audit_entry(
    *,
    run_id: str,
    patient_id: str,
    user_id: str,
    field_path: str,
    old_value: Any,
    new_value: Any,
    source_phrase: Optional[str],
    confidence: Optional[float],
    approval_method: str,
    status: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """
    Build the in-memory dict that the audit node will later persist.

    Shape mirrors `db_models.AuditEntry` plus a `status`/`reason` pair
    used by summarize_state counting. The audit node drops those two
    keys before insert (they are not columns).
    """
    entry: dict[str, Any] = {
        "run_id": run_id,
        "patient_id": patient_id,
        "user_id": user_id,
        "field_path": field_path,
        "old_value": old_value,
        "new_value": new_value,
        "source_phrase": source_phrase,
        "confidence": confidence,
        "approval_method": approval_method,
        "created_at": utc_now_iso(),
        "status": status,       # "applied" or "skipped"
        "reason": reason,       # None when status == "applied"
    }
    return entry


def _append_to_list(
    *,
    target: dict[str, Any],
    field_path: str,
    new_value: Any,
) -> tuple[Any, Any]:
    """
    Apply append semantics to a list-valued field. Returns
    `(old_value, applied_value)`.

    Rules:
      * If no value exists yet, create a new list.
      * If the existing value is a list, append. `new_value` may itself
        be a list (multiple items) or a single item; we normalise to
        "extend" in the list case to support both.
      * If the existing value is not a list, we REFUSE and raise
        TypeError — silently converting a scalar to a list would
        destroy the old data. The caller catches this and records a
        skipped entry.
    """
    existing = get_nested(target, field_path, default=None)

    if existing is None:
        new_list: list[Any] = list(new_value) if isinstance(new_value, list) else [new_value]
        set_nested(target, field_path, new_list)
        return None, list(new_list)

    if isinstance(existing, list):
        updated = list(existing)  # copy so the audit can compare old/new
        if isinstance(new_value, list):
            updated.extend(new_value)
        else:
            updated.append(new_value)
        set_nested(target, field_path, updated)
        return list(existing), list(updated)

    raise TypeError(
        f"append target at path is not a list (type={type(existing).__name__})"
    )


def _apply_single_update(
    *,
    final_json: dict[str, Any],
    update: UpdateObject,
) -> tuple[Any, Any]:
    """
    Apply one update to `final_json` in place. Returns `(old_value, new_value)`.

    Caller is responsible for skipping AMBIGUOUS paths; this function
    assumes a concrete field_path.
    """
    field_path = update.field_path
    new_value = update.new_value

    if _is_append_path(field_path):
        return _append_to_list(
            target=final_json,
            field_path=field_path,
            new_value=new_value,
        )

    old_value = get_nested(final_json, field_path, default=None)
    set_nested(final_json, field_path, new_value)
    return old_value, new_value


# --------------------------------------------------------------------------- #
# Node entry point
# --------------------------------------------------------------------------- #
def merge_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: merge approved updates into a copy of master_json.

    Returns a partial state dict (LangGraph merges it back). Never
    mutates `state`.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    user_id = state.get("user_id")
    master_json = state.get("master_json") or {}

    if not isinstance(master_json, dict):
        # An earlier node produced something non-dict for master_json.
        # This is a programmer error, not a user-caused one — raise so
        # the pipeline marks the run failed rather than silently writing
        # a degraded document.
        raise TypeError("master_json must be a dict at merge time")

    if not isinstance(run_id, str) or not isinstance(patient_id, str):
        raise ValueError("merge requires run_id and patient_id on state")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("merge requires user_id on state")

    # Combine the two approved sources. `auto_approved` is everything
    # the confidence router passed through; `approved_updates` is the
    # human-decisioned list (may be absent for runs with no flagged
    # items). Order matters only for deterministic audit-entry order;
    # auto_approved first is fine.
    auto_approved = state.get("auto_approved") or []
    approved_updates = state.get("approved_updates") or []

    if not isinstance(auto_approved, list) or not isinstance(approved_updates, list):
        raise TypeError("auto_approved and approved_updates must be lists")

    total_incoming = len(auto_approved) + len(approved_updates)
    if total_incoming > _MAX_UPDATES_PER_MERGE:
        # Refuse rather than process a runaway batch. The pipeline will
        # mark the run failed via the caller's try/except.
        raise ValueError(
            f"merge received {total_incoming} updates, "
            f"exceeds cap of {_MAX_UPDATES_PER_MERGE}"
        )

    # Deep copy so the original master_json on state is never mutated,
    # even transitively through nested dicts.
    final_json: dict[str, Any] = copy.deepcopy(master_json)

    audit_entries: list[dict[str, Any]] = []
    merged_count = 0
    skipped_count = 0

    def _process(updates: list[Any], approval_method: str) -> None:
        nonlocal merged_count, skipped_count
        for raw in updates:
            validated = _coerce_to_update_object(raw)
            if validated is None:
                audit_entries.append(
                    _make_audit_entry(
                        run_id=run_id,
                        patient_id=patient_id,
                        user_id=user_id,
                        field_path="<invalid>",
                        old_value=None,
                        new_value=None,
                        source_phrase=None,
                        confidence=None,
                        approval_method=approval_method,
                        status="skipped",
                        reason="validation_failed",
                    )
                )
                skipped_count += 1
                continue

            if validated.field_path == AMBIGUOUS_FIELD_PATH:
                # An AMBIGUOUS update must have been resolved to a
                # concrete path during human review. If it reaches merge,
                # something upstream misrouted it — skip rather than
                # guess where it should land.
                audit_entries.append(
                    _make_audit_entry(
                        run_id=run_id,
                        patient_id=patient_id,
                        user_id=user_id,
                        field_path=AMBIGUOUS_FIELD_PATH,
                        old_value=None,
                        new_value=validated.new_value,
                        source_phrase=validated.source_phrase,
                        confidence=validated.confidence,
                        approval_method=approval_method,
                        status="skipped",
                        reason="ambiguous_path",
                    )
                )
                skipped_count += 1
                continue

            try:
                old_value, applied_value = _apply_single_update(
                    final_json=final_json,
                    update=validated,
                )
            except (TypeError, ValueError) as exc:
                # Path collisions (e.g. trying to descend into a scalar)
                # or append-on-non-list. Skip this update only.
                audit_entries.append(
                    _make_audit_entry(
                        run_id=run_id,
                        patient_id=patient_id,
                        user_id=user_id,
                        field_path=validated.field_path,
                        old_value=None,
                        new_value=validated.new_value,
                        source_phrase=validated.source_phrase,
                        confidence=validated.confidence,
                        approval_method=approval_method,
                        status="skipped",
                        reason=f"apply_error:{type(exc).__name__}",
                    )
                )
                skipped_count += 1
                continue

            audit_entries.append(
                _make_audit_entry(
                    run_id=run_id,
                    patient_id=patient_id,
                    user_id=user_id,
                    field_path=validated.field_path,
                    old_value=old_value,
                    new_value=applied_value,
                    source_phrase=validated.source_phrase,
                    confidence=validated.confidence,
                    approval_method=approval_method,
                    status="applied",
                    reason=None,
                )
            )
            merged_count += 1

    _process(auto_approved, APPROVAL_METHOD_AUTO)
    _process(approved_updates, APPROVAL_METHOD_HUMAN)

    # One log line with counts only — no field paths, no values.
    logger.info(
        "merge complete: run_id=%s applied=%d skipped=%d",
        run_id,
        merged_count,
        skipped_count,
    )

    # Merge previous audit_entries (if any) with the new ones so earlier
    # nodes that emitted audit rows (e.g. a future parser step) are not
    # overwritten. LangGraph's partial-update semantics do not deep-merge
    # lists — we do it explicitly here.
    existing_audit = state.get("audit_entries") or []
    combined_audit = list(existing_audit) + audit_entries

    return {
        "final_json": final_json,
        "audit_entries": combined_audit,
    }


__all__ = ["merge_node"]
