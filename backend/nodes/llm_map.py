"""
llm_map node.

First LLM stage: takes the transcript and a pruned copy of the patient's
master JSON and produces a list of proposed field updates. Calls
Anthropic Claude via Amazon Bedrock InvokeModel using the Messages API.

Position in the REASSESSMENT graph:
    transcribe → LLM_MAP → llm_critic → confidence → ...

Inputs (from state):
    run_id, patient_id
    transcript:     str   — from the transcribe node
    master_json:    dict  — from load_patient_json

Outputs (merged into state):
    proposed_updates: list[dict]  — each item validated through
                                    `models.UpdateObject`; the raw model
                                    output is NEVER placed on state.

Security posture:
  * Every proposed update is validated through `models.UpdateObject`
    BEFORE it touches state. Malformed items are dropped with a
    counted reason (logged as a count only, never echoed). The LLM
    never gets a direct write to state.
  * Prompt inputs are built by `prompts.build_user_prompt`, which
    wraps user content in XML delimiters and neutralises delimiter-
    collision injection. We do NOT concatenate transcript text into
    the system prompt.
  * Response body is size-capped before JSON parsing and before
    iteration, so a runaway LLM reply cannot exhaust memory or make us
    validate tens of thousands of items.
  * Temperature / max_tokens / model id are read from settings each
    call — an ops change takes effect on the next run without a
    redeploy.
  * On Bedrock transport or content failure we RAISE. A silent empty
    proposal list would be indistinguishable from "no clinical
    content in dictation" — this would cause caregiver statements to
    never reach the record.
  * boto3 is sync; we always wrap the invocation in `asyncio.to_thread`.
  * Uses the `extract_schema_paths` helper over master_json to
    constrain the LLM to real field paths. Schema paths are
    deterministic (sorted) so the prompt is cache-friendly.
  * Never logs transcript text, proposed content, or the raw LLM
    response body. Counts only.
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
from models import MAX_DECISIONS_PER_SUBMISSION, UpdateObject
from prompts import (
    MAX_PROPOSED_UPDATES,
    MAX_TRANSCRIPT_CHARS,
    SYSTEM_PROMPT_MAPPER,
    build_user_prompt,
    extract_schema_paths,
    select_relevant_sections,
)
from state import PipelineState
from utils import safe_json_parse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# Anthropic API version used in every Bedrock InvokeModel body. Pinned
# because Bedrock's accepted value has historically changed across
# runtime releases.
_ANTHROPIC_VERSION: Final[str] = "bedrock-2023-05-31"

# Cap the raw response bytes before parsing. max_tokens=8192 yields at
# most ~32 KB of UTF-8; 1 MiB is a generous ceiling that still refuses
# a pathological body.
_MAX_RESPONSE_BYTES: Final[int] = 1 * 1024 * 1024

# Hard cap on items we will even attempt to validate. Mirrors the
# API-layer submission ceiling so a runaway LLM reply can't push the
# merge node past its own guardrail.
_MAX_ITEMS_TO_VALIDATE: Final[int] = MAX_DECISIONS_PER_SUBMISSION


def _invoke_bedrock_sync(
    *,
    model_id: str,
    body: str,
) -> dict[str, Any]:
    """
    Blocking Bedrock invoke. Runs on a worker thread.

    Returns the parsed response envelope (Anthropic Messages API shape).
    Raises `ClientError` / `BotoCoreError` on transport failure — those
    bubble to `llm_map_node` where they are caught and re-raised with
    PHI-safe context.
    """
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


def _extract_text_from_envelope(envelope: dict[str, Any]) -> str:
    """
    Pull the assistant's text out of an Anthropic Messages-API response.

    Envelope shape (relevant parts):
        {
          "content": [{"type": "text", "text": "..."}],
          "stop_reason": "...",
          ...
        }
    """
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


def _coerce_and_validate(items: list[Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    Turn a raw list from the LLM into a list of validated UpdateObject
    dicts. Returns `(validated_list, drop_counts)`; drop_counts buckets
    by reason for PHI-safe logging.
    """
    validated: list[dict[str, Any]] = []
    drops: dict[str, int] = {
        "not_dict": 0,
        "validation_failed": 0,
    }

    for item in items[:_MAX_ITEMS_TO_VALIDATE]:
        if not isinstance(item, dict):
            drops["not_dict"] += 1
            continue
        try:
            obj = UpdateObject.model_validate(item)
        except ValidationError:
            drops["validation_failed"] += 1
            continue
        validated.append(obj.model_dump())

    return validated, drops


async def llm_map_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: run the mapper LLM call.

    Returns `{"proposed_updates": [...]}` or raises on transport /
    parse / no-text failure.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    transcript = state.get("transcript")
    master_json = state.get("master_json")

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("llm_map requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("llm_map requires patient_id on state")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("llm_map requires a non-empty transcript")
    if not isinstance(master_json, dict):
        raise TypeError("llm_map requires master_json as a dict")
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        # Defensive re-check. `build_user_prompt` will also enforce
        # this, but failing fast here gives a cleaner error surface.
        raise ValueError("transcript exceeds mapper input cap")

    settings = get_settings()

    # Build prompt inputs.
    filtered = select_relevant_sections(transcript, master_json)
    schema_paths = extract_schema_paths(master_json)
    user_prompt = build_user_prompt(
        transcript,
        filtered,
        schema_paths,
        prefiltered=True,
    )

    request_body = json.dumps(
        {
            "anthropic_version": _ANTHROPIC_VERSION,
            "max_tokens": settings.BEDROCK_MAX_TOKENS,
            "temperature": settings.BEDROCK_TEMPERATURE,
            "system": SYSTEM_PROMPT_MAPPER,
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
        "llm_map start: run_id=%s patient_id=%s transcript_chars=%d schema_paths=%d",
        run_id,
        patient_id,
        len(transcript),
        len(schema_paths),
    )

    try:
        envelope = await _invoke_bedrock(
            model_id=settings.BEDROCK_MODEL_ID,
            body=request_body,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        logger.error("llm_map ClientError: run_id=%s code=%s", run_id, code)
        raise RuntimeError(f"llm_map bedrock error: {code}") from exc
    except BotoCoreError as exc:
        logger.error(
            "llm_map transport error: run_id=%s err=%s",
            run_id,
            type(exc).__name__,
        )
        raise RuntimeError(
            f"llm_map transport error: {type(exc).__name__}"
        ) from exc

    text = _extract_text_from_envelope(envelope)
    parsed = safe_json_parse(text)

    if parsed is None:
        # Could not coerce to a list/dict. The LLM violated the "output
        # only a JSON array" contract. Raise — we do not want to
        # silently emit an empty updates list here.
        raise RuntimeError("llm_map output is not parseable JSON")

    if isinstance(parsed, dict):
        # Tolerate wrappers like {"updates": [...]} — some providers
        # occasionally wrap the array despite our instruction.
        inner = None
        for key in ("updates", "proposed_updates", "items"):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                inner = candidate
                break
        if inner is None:
            raise RuntimeError("llm_map output dict had no recognised list key")
        parsed = inner

    if not isinstance(parsed, list):
        raise RuntimeError("llm_map output is not a list")

    if len(parsed) > MAX_PROPOSED_UPDATES:
        # Runaway response. Truncating here would hide a bug; raise so
        # the run fails and operators see it.
        raise RuntimeError(
            f"llm_map returned {len(parsed)} items, "
            f"exceeds cap {MAX_PROPOSED_UPDATES}"
        )

    validated, drops = _coerce_and_validate(parsed)

    logger.info(
        "llm_map done: run_id=%s patient_id=%s validated=%d drop_not_dict=%d drop_invalid=%d",
        run_id,
        patient_id,
        len(validated),
        drops["not_dict"],
        drops["validation_failed"],
    )

    return {"proposed_updates": validated}


__all__ = ["llm_map_node"]
