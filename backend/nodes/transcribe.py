"""
Transcribe node.

Runs Amazon Transcribe Medical on the audio file uploaded by the
caregiver and attaches the plain-text transcript (plus segment metadata)
to pipeline state.

Position in the REASSESSMENT graph:
    load_patient_json → TRANSCRIBE → llm_map → llm_critic → ...

Inputs (from state):
    run_id, patient_id
    audio_s3_key: str  — S3 object key to transcribe (must be in our bucket)

Outputs (merged into state):
    transcript: str
    transcript_segments: list[dict]  — the raw `items` + `speaker_labels`
                                        from the Transcribe output JSON.

Security posture:
  * Per-job unique name derived from run_id plus a UUID, so two
    reassessments of the same patient cannot collide and so a replay
    attack cannot reuse a prior job name to hijack an output.
  * Transcribe writes its output JSON to OUR bucket under a
    `transcripts/` prefix. We pass `OutputBucketName` and
    `OutputKey` explicitly — NEVER let Transcribe use its managed
    output bucket (that bucket is not PHI-hardened).
  * Server-side encryption is set on the output using the S3 default
    bucket KMS key (`OutputEncryptionKMSKeyId` is not set — AWS falls
    back to the bucket's default encryption, which must be configured
    as SSE-KMS at bucket provisioning time).
  * The audio_s3_key is re-validated against the allowlisted prefixes
    and against path-traversal tokens. A caller that constructs state
    without going through the API layer still cannot feed Transcribe
    an arbitrary bucket object.
  * Poll loop has a hard timeout (`TRANSCRIBE_TIMEOUT_SECONDS`) so a
    stuck AWS job cannot leak worker slots forever. On timeout we
    attempt a best-effort job deletion and then raise.
  * On completion we DELETE the Transcribe job itself (not the output
    JSON — the audit trail keeps that). Job names are a limited AWS
    namespace and accumulated jobs leak patient identifiers as names.
  * Transcribe returns JSON with `{"results": {"transcript": "..."}}`.
    We parse defensively: oversize output (>8 MiB) is refused, and if
    the `transcript` field is missing or not a string we raise rather
    than silently feeding an empty transcript to the LLM.
  * No transcript text, audio key, or AWS request-id is logged. Counts,
    run_id, and job status only.
  * All boto3 calls run in `asyncio.to_thread` so the async event loop
    is not blocked by boto's sync I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from aws_clients import get_s3_client, get_transcribe_client
from config import get_settings
from models import S3_KEY_ALLOWED_PREFIXES
from state import PipelineState
from utils import safe_json_parse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# A 60-minute dictation JSON is typically 0.5–2 MB. 8 MiB is a firm
# ceiling — anything larger almost certainly indicates a Transcribe
# output file that has been tampered with or a mis-configured job.
_MAX_TRANSCRIBE_OUTPUT_BYTES: Final[int] = 8 * 1024 * 1024

# Transcribe job names: 1–200 chars, alnum/hyphen/underscore/period.
# Ours are shorter — UUID hex is plenty.
_TRANSCRIBE_JOB_NAME_MAX: Final[int] = 200

# S3 MediaFormat hints from the object key extension. Transcribe
# requires this parameter explicitly; it will not sniff.
_EXT_TO_MEDIA_FORMAT: Final[dict[str, str]] = {
    ".mp3": "mp3",
    ".mp4": "mp4",
    ".m4a": "mp4",
    ".wav": "wav",
    ".flac": "flac",
    ".ogg": "ogg",
    ".amr": "amr",
    ".webm": "webm",
}

# Matches `models._validate_s3_key` — replicated here so the node is
# self-contained and defensive if models.py evolves.
_CONTROL_CHARS_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")


def _validate_audio_key(audio_key: str) -> None:
    """
    Re-check `audio_key` at the Transcribe boundary. We trust the API
    layer's validator but re-run the core checks here because passing
    an attacker-chosen key to StartMedicalTranscriptionJob could direct
    Transcribe at an arbitrary bucket object.
    """
    if not isinstance(audio_key, str) or not audio_key:
        raise ValueError("audio_s3_key is required")
    if ".." in audio_key or "//" in audio_key or audio_key.startswith("/"):
        raise ValueError("audio_s3_key has a forbidden path token")
    if _CONTROL_CHARS_RE.search(audio_key):
        raise ValueError("audio_s3_key contains control characters")
    if not any(audio_key.startswith(p) for p in S3_KEY_ALLOWED_PREFIXES):
        raise ValueError("audio_s3_key is outside the allowed prefix set")


def _infer_media_format(audio_key: str) -> str:
    """
    Return the MediaFormat Transcribe expects, based on the key
    extension. Raises ValueError on an unknown extension — we refuse to
    guess.
    """
    lower = audio_key.lower()
    for ext, fmt in _EXT_TO_MEDIA_FORMAT.items():
        if lower.endswith(ext):
            return fmt
    raise ValueError("audio_s3_key has an unsupported media extension")


def _build_job_name(run_id: str) -> str:
    """
    Build a per-run, per-call Transcribe job name. Prefix with
    `samni-` so jobs are identifiable in the AWS console. Append a
    fresh UUID4 hex so a retry within the same run does not collide
    with a still-pending earlier attempt.
    """
    suffix = uuid.uuid4().hex[:16]
    # run_id is a UUID; it is 36 chars including hyphens. Our prefix
    # + hyphen + run_id + hyphen + suffix is ~65 chars — well inside
    # the 200-char limit.
    name = f"samni-{run_id}-{suffix}"
    if len(name) > _TRANSCRIBE_JOB_NAME_MAX:
        raise ValueError("transcription job name exceeded AWS limit")
    return name


def _build_output_key(run_id: str, job_name: str) -> str:
    """
    Path for Transcribe's output JSON. Lives under the `transcripts/`
    prefix so S3 lifecycle / bucket policies treat it as PHI.
    """
    return f"transcripts/{run_id}/{job_name}.json"


# --------------------------------------------------------------------------- #
# boto wrappers — all run via asyncio.to_thread
# --------------------------------------------------------------------------- #
async def _start_transcription_job(
    *,
    job_name: str,
    audio_uri: str,
    media_format: str,
    output_bucket: str,
    output_key: str,
) -> None:
    settings = get_settings()
    client = get_transcribe_client()

    await asyncio.to_thread(
        client.start_medical_transcription_job,
        MedicalTranscriptionJobName=job_name,
        LanguageCode=settings.TRANSCRIBE_LANGUAGE_CODE,
        Specialty=settings.TRANSCRIBE_SPECIALTY,
        Type=settings.TRANSCRIBE_TYPE,
        MediaFormat=media_format,
        Media={"MediaFileUri": audio_uri},
        OutputBucketName=output_bucket,
        OutputKey=output_key,
    )


async def _get_job_status(job_name: str) -> dict[str, Any]:
    client = get_transcribe_client()
    response = await asyncio.to_thread(
        client.get_medical_transcription_job,
        MedicalTranscriptionJobName=job_name,
    )
    return response.get("MedicalTranscriptionJob", {})


async def _delete_job(job_name: str) -> None:
    """
    Best-effort cleanup. A failure here is never fatal — the output
    JSON has already been fetched (or we have already failed the run),
    and a leftover job in AWS is an operational annoyance, not a data
    or security issue.
    """
    client = get_transcribe_client()
    try:
        await asyncio.to_thread(
            client.delete_medical_transcription_job,
            MedicalTranscriptionJobName=job_name,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "transcribe job delete failed: %s",
            type(exc).__name__,
        )


async def _fetch_output_json(
    *,
    bucket: str,
    key: str,
) -> dict[str, Any]:
    """
    Fetch the Transcribe output JSON from our bucket. Size-capped read
    so a tampered output cannot exhaust memory.
    """
    client = get_s3_client()
    response = await asyncio.to_thread(
        client.get_object,
        Bucket=bucket,
        Key=key,
    )

    body = response.get("Body")
    if body is None:
        raise ValueError("transcribe output has no body")

    try:
        raw = await asyncio.to_thread(body.read, _MAX_TRANSCRIBE_OUTPUT_BYTES + 1)
    finally:
        # Always close the streaming body.
        try:
            body.close()
        except Exception:  # noqa: BLE001 — close failures are non-fatal
            pass

    if len(raw) > _MAX_TRANSCRIBE_OUTPUT_BYTES:
        raise ValueError("transcribe output exceeded size cap")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("transcribe output is not valid UTF-8") from exc

    parsed = safe_json_parse(text)
    if not isinstance(parsed, dict):
        raise ValueError("transcribe output is not a JSON object")
    return parsed


def _extract_transcript(output_json: dict[str, Any]) -> tuple[str, list[Any]]:
    """
    Pull the transcript string and the item list out of Transcribe's
    output JSON. Defensive about the schema: raises on missing/wrong
    types rather than returning empty values (an empty transcript
    silently producing no updates would be a bad failure mode).
    """
    results = output_json.get("results")
    if not isinstance(results, dict):
        raise ValueError("transcribe output has no results block")

    transcripts = results.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        raise ValueError("transcribe output has no transcripts list")

    text_parts: list[str] = []
    for entry in transcripts:
        if not isinstance(entry, dict):
            continue
        piece = entry.get("transcript")
        if isinstance(piece, str):
            text_parts.append(piece)
    if not text_parts:
        raise ValueError("transcribe output transcripts is empty")
    transcript = " ".join(text_parts).strip()
    if not transcript:
        raise ValueError("transcribe output transcript is blank")

    # `items` is the per-token timing/confidence array. Optional but
    # useful for downstream grounding. Keep if it is a list; otherwise
    # return an empty list rather than raising — missing items is a
    # minor degradation, not a failure.
    items = results.get("items")
    items_list: list[Any] = items if isinstance(items, list) else []

    return transcript, items_list


# --------------------------------------------------------------------------- #
# Poll loop
# --------------------------------------------------------------------------- #
async def _poll_until_done(job_name: str) -> dict[str, Any]:
    """
    Poll the Transcribe job until COMPLETED, FAILED, or the overall
    timeout elapses. Returns the final job dict.
    """
    settings = get_settings()
    interval = settings.TRANSCRIBE_POLL_INTERVAL_SECONDS
    deadline = time.monotonic() + settings.TRANSCRIBE_TIMEOUT_SECONDS

    while True:
        job = await _get_job_status(job_name)
        status = job.get("TranscriptionJobStatus")

        if status == "COMPLETED":
            return job
        if status == "FAILED":
            # FailureReason may contain user-friendly AWS text; log
            # only its general shape, never the full value (which
            # occasionally echoes the input URI).
            logger.warning(
                "transcribe job failed: job=%s reason_type=%s",
                job_name,
                type(job.get("FailureReason", "")).__name__,
            )
            raise RuntimeError("transcribe job failed")
        if status not in ("IN_PROGRESS", "QUEUED"):
            # Unknown status — refuse.
            raise RuntimeError("transcribe job returned unexpected status")

        if time.monotonic() >= deadline:
            raise TimeoutError("transcribe job exceeded timeout")

        await asyncio.sleep(interval)


# --------------------------------------------------------------------------- #
# Node entry point
# --------------------------------------------------------------------------- #
async def transcribe_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: run Transcribe Medical against `audio_s3_key` and
    return `transcript` + `transcript_segments` on state.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    audio_key = state.get("audio_s3_key")

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("transcribe requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("transcribe requires patient_id on state")

    _validate_audio_key(audio_key)  # type: ignore[arg-type]
    media_format = _infer_media_format(audio_key)  # type: ignore[arg-type]

    settings = get_settings()
    bucket = settings.S3_BUCKET
    job_name = _build_job_name(run_id)
    output_key = _build_output_key(run_id, job_name)
    audio_uri = f"s3://{bucket}/{audio_key}"

    logger.info(
        "transcribe start: run_id=%s patient_id=%s job=%s",
        run_id,
        patient_id,
        job_name,
    )

    try:
        await _start_transcription_job(
            job_name=job_name,
            audio_uri=audio_uri,
            media_format=media_format,
            output_bucket=bucket,
            output_key=output_key,
        )
        job = await _poll_until_done(job_name)
    except TimeoutError:
        # Best-effort cleanup of the orphaned job before we raise.
        await _delete_job(job_name)
        raise
    except ClientError as exc:
        # Don't leak response bodies. Surface only the AWS error code.
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        logger.error(
            "transcribe ClientError: run_id=%s job=%s code=%s",
            run_id,
            job_name,
            code,
        )
        await _delete_job(job_name)
        raise RuntimeError(f"transcribe client error: {code}") from exc
    except BotoCoreError as exc:
        await _delete_job(job_name)
        raise RuntimeError(
            f"transcribe transport error: {type(exc).__name__}"
        ) from exc

    # Transcribe has written output to our bucket at output_key. We
    # read from there (NOT from job.Transcript.TranscriptFileUri, which
    # can be a pre-signed URL). Using the canonical key ensures the
    # file we read is the one we own.
    actual_uri = ""
    transcript_block = job.get("Transcript") or {}
    if isinstance(transcript_block, dict):
        actual_uri = transcript_block.get("TranscriptFileUri") or ""

    expected_suffix = f"/{bucket}/{output_key}"
    if expected_suffix not in actual_uri:
        # Sanity check: if Transcribe's reported URI does not reference
        # our expected key, something is wrong — refuse to parse an
        # unknown file.
        await _delete_job(job_name)
        raise RuntimeError("transcribe output path mismatch")

    output_json = await _fetch_output_json(bucket=bucket, key=output_key)
    transcript, segments = _extract_transcript(output_json)

    # Best-effort: remove the job row in AWS so our job namespace stays
    # clean. The output JSON stays in S3 under our key.
    await _delete_job(job_name)

    logger.info(
        "transcribe done: run_id=%s patient_id=%s transcript_chars=%d segments=%d",
        run_id,
        patient_id,
        len(transcript),
        len(segments),
    )

    return {
        "transcript": transcript,
        "transcript_segments": segments,
    }


__all__ = ["transcribe_node"]
