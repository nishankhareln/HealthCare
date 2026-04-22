"""
llm_critic node.

Second LLM stage: re-reads the mapper's proposed updates against the
transcript and corrects mistakes in place. Never adds or drops items —
the critic must preserve list length so the downstream confidence
router and merge node can rely on ordering and count invariants.

Position in the REASSESSMENT graph:
    llm_map → LLM_CRITIC → confidence → human_review? → merge → ...

Inputs (from state):
    run_id, patient_id
    transcript:        str
    proposed_updates:  list[dict] — already validated by llm_map

Outputs (merged into state):
    verified_updates:  list[dict] — same length as proposed_updates;
                                    every entry validated through
                                    `models.UpdateObject`.

Security posture:
  * Same Bedrock / injection / size-cap posture as llm_map. See that
    module for detail.
  * Critic MUST preserve the input list length. If the LLM returns a
    shorter or longer list, we DO NOT silently pad or truncate. We
    fall back to the mapper's output (the "do no harm" path) and log
    that the critic was bypassed. The pipeline then proceeds normally
    — the flagged-threshold gate still separates high-confidence from
    low-confidence items.
  * Every returned item is re-validated through `UpdateObject`.
    Invalid items are REPLACED positionally with the mapper's original
    entry rather than dropped, so length is preserved and no
    caregiver statement silently vanishes.
  * The critic never has write access to state's `master_json`; it can
    only propose changes to the `proposed_updates` list.
  * Logs counts only — never transcript text, update content, or raw
    LLM output.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from aws_clients import get_bedrock_runtime_client
from config import get_settings
from models import UpdateObject
from prompts import (
    MAX_PROPOSED_UPDATES,
    MAX_TRANSCRIPT_CHARS,
    SYSTEM_PROMPT_CRITIC,
    build_critic_user_prompt,
    extract_schema_paths,
)
from state import PipelineState
from utils import safe_json_parse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety bounds — mirror llm_map
# --------------------------------------------------------------------------- #
_ANTHROPIC_VERSION: Final[str] = "bedrock-2023-05-31"
_MAX_RESPONSE_BYTES: Final[int] = 1 * 1024 * 1024


def _invoke_bedrock_sync(*, model_id: str, body: str) -> dict[str, Any]:
    client = get_bedrock_runtime_client()
    response = client.invoke_model(
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
        body=body.encode("utf-8"),
    )
    raw = response.get("body")
    if raw is None:
        raise RuntimeError("bedrock response missing body")

    try:
        data = raw.read(_MAX_RESPONSE_BYTES + 1)
    finally:
        close = getattr(raw, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — close failures non-fatal
                pass

    if len(data) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("bedrock response exceeded size cap")

    try:
        envelope = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("bedrock response is not valid JSON/UTF-8") from exc

    if not isinstance(envelope, dict):
        raise RuntimeError("bedrock response is not a JSON object")
    return envelope


async def _invoke_bedrock(*, model_id: str, body: str) -> dict[str, Any]:
    return await asyncio.to_thread(_invoke_bedrock_sync, model_id=model_id, body=body)


def _extract_text(envelope: dict[str, Any]) -> str:
    content = envelope.get("content")
    if not isinstance(content, list):
        raise RuntimeError("bedrock response has no content list")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    if not parts:
        raise RuntimeError("bedrock response has no text content")
    return "".join(parts)


def _revalidate_preserving_length(
    *,
    critic_items: list[Any],
    mapper_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """
    Re-validate critic output while preserving list length.

    For each position i:
      * If critic_items[i] parses to a valid UpdateObject, use it.
      * Otherwise fall back to mapper_items[i] (already validated by
        llm_map, so it is guaranteed to round-trip).

    Returns `(final_list, fallback_count)`.
    """
    final: list[dict[str, Any]] = []
    fallbacks = 0
    for idx, mapper_item in enumerate(mapper_items):
        critic_item = critic_items[idx] if idx < len(critic_items) else None
        if isinstance(critic_item, dict):
            try:
                final.append(UpdateObject.model_validate(critic_item).model_dump())
                continue
            except ValidationError:
                pass
        # Fall back to the mapper's entry.
        final.append(dict(mapper_item))
        fallbacks += 1
    return final, fallbacks


async def llm_critic_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: run the critic LLM pass.

    Returns `{"verified_updates": [...]}`. Same length as
    `proposed_updates`. Never raises on critic-output shape problems —
    falls back to the mapper's output. Raises only on Bedrock transport
    / envelope failures.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    transcript = state.get("transcript")
    proposed_updates = state.get("proposed_updates") or []
    master_json = state.get("master_json") or {}

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("llm_critic requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("llm_critic requires patient_id on state")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("llm_critic requires a non-empty transcript")
    if not isinstance(proposed_updates, list):
        raise TypeError("llm_critic requires proposed_updates as a list")
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise ValueError("transcript exceeds critic input cap")
    if len(proposed_updates) > MAX_PROPOSED_UPDATES:
        raise ValueError("proposed_updates exceeds critic input cap")

    # Empty input → nothing to critique. Skip the LLM call entirely;
    # this is a legitimate path when the transcript contained no
    # clinical content.
    if not proposed_updates:
        logger.info(
            "llm_critic skipped (empty input): run_id=%s patient_id=%s",
            run_id,
            patient_id,
        )
        return {"verified_updates": []}

    settings = get_settings()

    schema_paths = extract_schema_paths(master_json) if master_json else []
    user_prompt = build_critic_user_prompt(
        transcript,
        proposed_updates,
        schema_paths,
    )

    request_body = json.dumps(
        {
            "anthropic_version": _ANTHROPIC_VERSION,
            "max_tokens": settings.BEDROCK_MAX_TOKENS,
            "temperature": settings.BEDROCK_TEMPERATURE,
            "system": SYSTEM_PROMPT_CRITIC,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_prompt}],
                }
            ],
        },
        separators=(",", ":"),
    )

    logger.info(
        "llm_critic start: run_id=%s patient_id=%s proposed=%d",
        run_id,
        patient_id,
        len(proposed_updates),
    )

    try:
        envelope = await _invoke_bedrock(
            model_id=settings.BEDROCK_MODEL_ID,
            body=request_body,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        logger.error("llm_critic ClientError: run_id=%s code=%s", run_id, code)
        raise RuntimeError(f"llm_critic bedrock error: {code}") from exc
    except BotoCoreError as exc:
        logger.error(
            "llm_critic transport error: run_id=%s err=%s",
            run_id,
            type(exc).__name__,
        )
        raise RuntimeError(
            f"llm_critic transport error: {type(exc).__name__}"
        ) from exc

    try:
        text = _extract_text(envelope)
    except RuntimeError:
        # Envelope malformed — fall back to mapper output rather than
        # failing the whole run. The critic is an enhancer, not a gate.
        logger.warning(
            "llm_critic envelope malformed; falling back to mapper output: run_id=%s",
            run_id,
        )
        return {"verified_updates": [dict(u) for u in proposed_updates]}

    parsed = safe_json_parse(text)

    # Accept both a list and a `{"updates": [...]}` wrapper.
    if isinstance(parsed, dict):
        inner: list[Any] | None = None
        for key in ("updates", "proposed_updates", "verified_updates", "items"):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                inner = candidate
                break
        parsed = inner

    if not isinstance(parsed, list) or len(parsed) != len(proposed_updates):
        # Length violation OR not a list. The critic broke its contract.
        # Fall back rather than raise; the mapper output is still valid.
        logger.warning(
            "llm_critic output shape mismatch; falling back: run_id=%s "
            "expected_len=%d got_type=%s",
            run_id,
            len(proposed_updates),
            type(parsed).__name__,
        )
        return {"verified_updates": [dict(u) for u in proposed_updates]}

    verified, fallbacks = _revalidate_preserving_length(
        critic_items=parsed,
        mapper_items=proposed_updates,
    )

    logger.info(
        "llm_critic done: run_id=%s patient_id=%s verified=%d fallbacks=%d",
        run_id,
        patient_id,
        len(verified),
        fallbacks,
    )

    return {"verified_updates": verified}


__all__ = ["llm_critic_node"]
