"""
transcribe node — Amazon Transcribe Medical async batch job runner.

Position in the graph:
    load_patient_json → TRANSCRIBE → llm_map → ...

Why this lives on the backend rather than in Flutter:
  Flutter's WebSocket-based real-time transcription proved unreliable on
  the target devices, so we moved it server-side. The flow is now:

      1. Flutter records audio on-device.
      2. Flutter uploads the audio file to S3 (`audio/` prefix) using a
         presigned URL.
      3. Flutter calls POST /reassessment with the S3 key.
      4. Worker dispatches to LangGraph; this node runs.
      5. We start an Amazon Transcribe Medical job pointed at that S3
         object, telling Transcribe to write its result back into our
         own bucket under `transcripts/{run_id}.json`.
      6. We poll the job until it completes (or until the timeout).
      7. We read the transcript text out of the result JSON and put it
         on `state.transcript` for `llm_map` to consume.

Inputs (from state):
    audio_s3_key: str
    run_id: str
    patient_id: str

Outputs (merged into state):
    transcript: str
    transcript_s3_key: str

Security posture:
  * The audio file and the resulting transcript both contain PHI. We
    NEVER log either. Only byte sizes, durations, and IDs.
  * The Transcribe job name is `samni-{run_id}-{short_uuid}`. The UUID
    suffix prevents collisions if a job is re-driven by SQS after a
    crash (Transcribe forbids re-using a job name).
  * The job is configured to write its output INTO our own S3 bucket
    via `OutputBucketName`. We never use Transcribe's "Amazon-managed"
    output location, which would leave PHI sitting in an AWS-controlled
    bucket outside our KMS scope.
  * `OutputKey` always lives under `transcripts/`. The reassessment
    pipeline's `transcripts/{run_id}.json` shape is part of the
    contract validated by `models.S3_KEY_ALLOWED_PREFIXES`.
  * Polling is bounded by `TRANSCRIBE_TIMEOUT_SECONDS` (default 5 min).
    Exceeding raises and the run goes to `failed` rather than the
    worker hanging indefinitely.
  * If a transcript object already exists at the expected S3 key (e.g.
    a previous SQS redelivery for the same run already produced one),
    we short-circuit and reuse it instead of re-running Transcribe. This
    is a cost + idempotency win.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from aws_clients import get_s3_client, get_transcribe_client
from config import get_settings
from models import S3_KEY_ALLOWED_PREFIXES
from state import PipelineState
from utils import safe_json_parse

logger = logging.getLogger(__name__)


# Hard cap on the size of a transcript JSON we will accept from S3. Real
# caregiver dictations are KBs of text. Anything past 4 MiB is suspicious.
_MAX_TRANSCRIPT_BYTES: Final[int] = 4 * 1024 * 1024

# Transcribe Medical job name regex: letters, numbers, dot, dash, underscore.
_JOB_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _build_job_name(run_id: str) -> str:
    """
    Build a unique Transcribe Medical job name for this invocation.

    Includes a short UUID to make redeliveries idempotent (Transcribe
    refuses to start a new job if a job with the same name already
    exists). The run_id is included for operator-level traceability.
    """
    suffix = uuid.uuid4().hex[:8]
    name = f"samni-{run_id}-{suffix}"
    if not _JOB_NAME_RE.match(name):
        # Defensive: if run_id ever loosens its format, refuse rather
        # than send Transcribe a malformed job name.
        raise ValueError("derived Transcribe job name is invalid")
    return name


def _expected_transcript_key(run_id: str) -> str:
    """The S3 key where we tell Transcribe to write the result JSON."""
    return f"transcripts/{run_id}.json"


def _media_format_from_key(s3_key: str) -> str:
    """
    Infer the Transcribe Medical `MediaFormat` from the file extension.

    Transcribe Medical accepts: mp3, mp4, wav, flac, ogg, amr, webm, m4a.
    Default to m4a (the iOS default Flutter records to).
    """
    ext = s3_key.rsplit(".", 1)[-1].lower() if "." in s3_key else ""
    allowed = {"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"}
    if ext in allowed:
        # Transcribe wants 'mp4' for m4a containers.
        return "mp4" if ext == "m4a" else ext
    return "mp4"


async def _transcript_text_if_cached(s3_key: str) -> str | None:
    """
    If a transcript JSON already exists at `s3_key`, return its text.

    Used for SQS redelivery idempotency: if we already produced a
    transcript for this run on a previous attempt, do not re-run
    Transcribe Medical (which would fail with a duplicate-job error
    anyway, and burn billable seconds).
    """
    settings = get_settings()
    s3 = get_s3_client()
    try:
        head = await asyncio.to_thread(
            s3.head_object, Bucket=settings.S3_BUCKET, Key=s3_key
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    size = int(head.get("ContentLength") or 0)
    if size <= 0 or size > _MAX_TRANSCRIPT_BYTES:
        return None

    obj = await asyncio.to_thread(
        s3.get_object, Bucket=settings.S3_BUCKET, Key=s3_key
    )
    body: bytes = await asyncio.to_thread(obj["Body"].read)
    payload = safe_json_parse(body.decode("utf-8"))
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, dict):
            transcripts = results.get("transcripts")
            if isinstance(transcripts, list) and transcripts:
                first = transcripts[0]
                if isinstance(first, dict):
                    text = first.get("transcript")
                    if isinstance(text, str) and text.strip():
                        return text
    return None


async def transcribe_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: turn `audio_s3_key` into `transcript` text.
    """
    run_id = state.get("run_id")
    audio_s3_key = state.get("audio_s3_key")
    patient_id = state.get("patient_id")

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("transcribe requires run_id on state")
    if not isinstance(audio_s3_key, str) or not audio_s3_key:
        raise ValueError("transcribe requires audio_s3_key on state")
    # The reassessment API path validates this prefix already; defense
    # in depth in case state was constructed by a different caller.
    if not any(audio_s3_key.startswith(p) for p in S3_KEY_ALLOWED_PREFIXES):
        raise ValueError("audio_s3_key uses a disallowed S3 prefix")

    settings = get_settings()
    transcript_s3_key = _expected_transcript_key(run_id)

    # Idempotency: if a previous worker invocation for this run already
    # produced a transcript, reuse it.
    cached = await _transcript_text_if_cached(transcript_s3_key)
    if cached is not None:
        logger.info(
            "transcribe: cached transcript reused run_id=%s patient_id=%s len=%d",
            run_id,
            patient_id,
            len(cached),
        )
        return {"transcript": cached, "transcript_s3_key": transcript_s3_key}

    transcribe = get_transcribe_client()
    job_name = _build_job_name(run_id)
    media_uri = f"s3://{settings.S3_BUCKET}/{audio_s3_key}"

    try:
        await asyncio.to_thread(
            transcribe.start_medical_transcription_job,
            MedicalTranscriptionJobName=job_name,
            LanguageCode=settings.TRANSCRIBE_LANGUAGE_CODE,
            Specialty=settings.TRANSCRIBE_SPECIALTY,
            Type=settings.TRANSCRIBE_TYPE,
            MediaFormat=_media_format_from_key(audio_s3_key),
            Media={"MediaFileUri": media_uri},
            OutputBucketName=settings.S3_BUCKET,
            OutputKey=transcript_s3_key,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception(
            "transcribe: failed to start job run_id=%s patient_id=%s err=%s",
            run_id,
            patient_id,
            type(exc).__name__,
        )
        raise

    logger.info(
        "transcribe: job started run_id=%s job_name=%s media=%s",
        run_id,
        job_name,
        media_uri,
    )

    # Poll until the job leaves IN_PROGRESS, bounded by the timeout.
    poll_interval = settings.TRANSCRIBE_POLL_INTERVAL_SECONDS
    deadline = settings.TRANSCRIBE_TIMEOUT_SECONDS
    waited = 0
    while waited < deadline:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        try:
            resp = await asyncio.to_thread(
                transcribe.get_medical_transcription_job,
                MedicalTranscriptionJobName=job_name,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.exception(
                "transcribe: poll failed run_id=%s job_name=%s err=%s",
                run_id,
                job_name,
                type(exc).__name__,
            )
            raise
        job = resp.get("MedicalTranscriptionJob") or {}
        status = job.get("TranscriptionJobStatus")
        if status == "COMPLETED":
            break
        if status == "FAILED":
            reason = job.get("FailureReason") or "unknown"
            raise RuntimeError(
                f"Transcribe Medical job failed: "
                f"{reason if len(reason) <= 256 else reason[:256] + '...'}"
            )
    else:
        raise TimeoutError(
            f"Transcribe Medical job did not complete within "
            f"{deadline}s (run_id={run_id})"
        )

    # Job succeeded. Read the result JSON we instructed it to write into
    # our bucket; never trust an Amazon-supplied URI.
    s3 = get_s3_client()
    obj = await asyncio.to_thread(
        s3.get_object, Bucket=settings.S3_BUCKET, Key=transcript_s3_key
    )
    body: bytes = await asyncio.to_thread(obj["Body"].read)
    if len(body) > _MAX_TRANSCRIPT_BYTES:
        raise ValueError("transcript result exceeds size cap")

    payload = safe_json_parse(body.decode("utf-8"))
    transcript_text: str | None = None
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, dict):
            transcripts = results.get("transcripts")
            if isinstance(transcripts, list) and transcripts:
                first = transcripts[0]
                if isinstance(first, dict):
                    text = first.get("transcript")
                    if isinstance(text, str) and text.strip():
                        transcript_text = text

    if transcript_text is None:
        raise ValueError("transcript result JSON did not contain text")

    logger.info(
        "transcribe: complete run_id=%s len=%d wait_s=%d",
        run_id,
        len(transcript_text),
        waited,
    )

    return {
        "transcript": transcript_text,
        "transcript_s3_key": transcript_s3_key,
    }


__all__ = ["transcribe_node"]
