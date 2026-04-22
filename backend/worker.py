"""
SQS background worker.

Runs as a long-lived async process (or as many processes as
WORKER_CONCURRENCY requires). Long-polls the pipeline SQS queue, runs
the requested LangGraph pipeline for each message, updates the
`pipeline_runs` row and fires SNS notifications at terminal transitions.

Lifecycle per message:
    1. Receive (long-poll).
    2. Parse the body as `models.SQSMessage`. Refuse any malformed
       message — a malformed message is deleted with an error log (see
       "Poison messages" below).
    3. Update `pipeline_runs` row to `running`, record started_at.
    4. Build `PipelineState` via `new_pipeline_state(...)`.
    5. `await graph.ainvoke(state, config=run_config(run_id))`.
    6. Inspect the final state:
         * If LangGraph reports an interrupt (human_review paused),
           update the row to `waiting_review` and persist the flagged
           list so /review/{patient_id} can resume.
         * If the graph completed, update the row to `complete` with
           completed_at and the output_pdf_s3_key (if any).
    7. Fire the appropriate SNS notification.
    8. Delete the SQS message on success. Leave it for re-delivery on
       transient failures (the queue's redrive policy will eventually
       dead-letter it).

Poison messages:
    A message whose body does not parse as `SQSMessage` cannot be
    retried into success — retrying it just consumes worker cycles
    until the queue's redrive hits. We DELETE such messages
    immediately with a WARNING log (run_id is unknown, so we log the
    message id and a generic failure reason). Genuine infrastructure
    failures (DB down, Bedrock down) are treated as transient and the
    message is NOT deleted.

Security posture:
  * `SQSMessage` is re-validated at the worker boundary — the queue
    is a trust boundary (anyone with SQS:SendMessage can publish),
    so every field goes through the regex / length / prefix checks.
  * Failed graph execution never leaves a run in `running`. Either
    we transition it to `failed` with a bounded error, or we raise
    and the message re-drives (the next delivery can see a run that
    says `running` and decide whether to retry or mark failed — we
    assume a simple "always fail-forward" model for now and mark
    `failed` before bubbling).
  * No PHI in logs. Message bodies, transcript text, flagged update
    content — none of it gets logged. Counts, run_id, status, message
    id, and error TYPE only.
  * Error text written to `pipeline_runs.error` is bounded by
    `state.mark_status`. We never write raw exception strings (which
    can carry traceback fragments) unbounded.
  * SNS failures are non-fatal — `notifications.send_notification`
    already returns bool. A push failure does not reverse the DB
    transition.
  * Concurrent message processing is bounded by an `asyncio.Semaphore`
    sized to `WORKER_CONCURRENCY`; the ReceiveMessage call requests
    up to that many at once.
  * Shutdown is co-operative: a `stop_event` is set by a signal
    handler; the poll loop finishes its current batch and exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from typing import Any, Optional

# Windows-only: psycopg3's async pool (used by the LangGraph checkpointer)
# cannot run on the ProactorEventLoop that Python 3.8+ installs by default
# on Windows. The SelectorEventLoop is required. No-op on Linux, which is
# where production runs — the Dockerfile base is debian-slim.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from botocore.exceptions import BotoCoreError, ClientError
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy import select

from aws_clients import get_sqs_client
from config import configure_logging, get_settings
from database import get_db_session, init_db, shutdown_db
from db_models import (
    PIPELINE_STATUS_COMPLETE,
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_RUNNING,
    PIPELINE_STATUS_WAITING_REVIEW,
    PipelineRun,
)
from models import SQSMessage
from notifications import (
    notify_pipeline_complete,
    notify_pipeline_failed,
    notify_review_required,
)
from pipeline import (
    get_graph_for,
    init_pipeline,
    run_config,
    shutdown_pipeline,
)
from state import (
    PIPELINE_INTAKE,
    PIPELINE_REASSESSMENT,
    new_pipeline_state,
    summarize_state,
)
from utils import utc_now

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# SQS ReceiveMessage raw body is capped at 256 KB by AWS, but a sane
# worker should also refuse anything absurdly large as a defence against
# a tampered message.
_MAX_BODY_BYTES: int = 128 * 1024

# How long the worker sleeps between polls if the queue returns 0
# messages without us long-polling (it shouldn't — wait_time is ≥1 —
# but this is a belt against a misconfigured queue that disables
# long polling).
_IDLE_BACKOFF_SECONDS: float = 1.0


# --------------------------------------------------------------------------- #
# SQS helpers (sync boto3 invoked via asyncio.to_thread)
# --------------------------------------------------------------------------- #
async def _receive_messages(max_messages: int) -> list[dict[str, Any]]:
    settings = get_settings()
    client = get_sqs_client()
    response = await asyncio.to_thread(
        client.receive_message,
        QueueUrl=settings.SQS_QUEUE_URL,
        MaxNumberOfMessages=max(1, min(10, max_messages)),
        WaitTimeSeconds=settings.SQS_WAIT_TIME_SECONDS,
        VisibilityTimeout=settings.SQS_VISIBILITY_TIMEOUT,
        AttributeNames=["All"],
        MessageAttributeNames=["All"],
    )
    return response.get("Messages", []) or []


async def _delete_message(receipt_handle: str) -> None:
    settings = get_settings()
    client = get_sqs_client()
    try:
        await asyncio.to_thread(
            client.delete_message,
            QueueUrl=settings.SQS_QUEUE_URL,
            ReceiptHandle=receipt_handle,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "sqs delete failed: %s",
            type(exc).__name__,
        )


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
async def _mark_run_running(msg: SQSMessage) -> None:
    """
    Move pipeline_runs row for this run_id to `running`. If the row is
    already in a terminal state (complete/failed) we do NOT regress it
    — a duplicate SQS delivery should not resurrect a finished run.
    """
    async with get_db_session() as session:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.run_id == msg.run_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        row: Optional[PipelineRun] = result.scalar_one_or_none()
        if row is None:
            raise LookupError(
                f"pipeline_runs row missing for run_id={msg.run_id}"
            )
        if row.status in (
            PIPELINE_STATUS_COMPLETE,
            PIPELINE_STATUS_FAILED,
        ):
            # Duplicate delivery of a message we already processed.
            raise RuntimeError("run already terminal; ignore delivery")

        row.status = PIPELINE_STATUS_RUNNING
        if row.started_at is None:
            row.started_at = utc_now()


async def _mark_run_waiting_review(
    *,
    run_id: str,
    flagged: list[dict[str, Any]],
) -> None:
    async with get_db_session() as session:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.run_id == run_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        row: Optional[PipelineRun] = result.scalar_one_or_none()
        if row is None:
            raise LookupError(
                f"pipeline_runs row missing for run_id={run_id}"
            )
        row.status = PIPELINE_STATUS_WAITING_REVIEW
        row.flagged_updates = flagged


async def _mark_run_complete(
    *,
    run_id: str,
    output_pdf_s3_key: Optional[str],
) -> None:
    async with get_db_session() as session:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.run_id == run_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        row: Optional[PipelineRun] = result.scalar_one_or_none()
        if row is None:
            raise LookupError(
                f"pipeline_runs row missing for run_id={run_id}"
            )
        row.status = PIPELINE_STATUS_COMPLETE
        row.output_pdf_s3_key = output_pdf_s3_key
        row.flagged_updates = None
        row.completed_at = utc_now()


async def _mark_run_failed(*, run_id: str, error_text: str) -> None:
    # Bound the error text (same pattern as state.mark_status). Keep
    # the message opaque — no traceback, no PHI.
    max_len = 2000
    safe = (
        error_text
        if len(error_text) <= max_len
        else error_text[: max_len - 12] + "...truncated"
    )
    async with get_db_session() as session:
        stmt = (
            select(PipelineRun)
            .where(PipelineRun.run_id == run_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        row: Optional[PipelineRun] = result.scalar_one_or_none()
        if row is None:
            logger.warning(
                "cannot mark run failed: row missing run_id=%s", run_id
            )
            return
        row.status = PIPELINE_STATUS_FAILED
        row.error = safe
        row.completed_at = utc_now()


# --------------------------------------------------------------------------- #
# Message parsing and dispatch
# --------------------------------------------------------------------------- #
def _parse_body(raw_body: Any) -> SQSMessage:
    """
    Decode and validate a raw SQS message body. Raises ValueError on
    any failure; the caller deletes the message (poison).
    """
    if not isinstance(raw_body, str) or not raw_body:
        raise ValueError("empty or non-string message body")
    if len(raw_body) > _MAX_BODY_BYTES:
        raise ValueError("message body exceeds size cap")
    try:
        doc = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError("message body is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise ValueError("message body is not a JSON object")
    try:
        return SQSMessage.model_validate(doc)
    except ValidationError as exc:
        raise ValueError(
            f"message body failed schema validation: {type(exc).__name__}"
        ) from exc


def _has_interrupt(result: Any) -> tuple[bool, list[dict[str, Any]]]:
    """
    Inspect a graph result to decide whether it paused on human_review.

    LangGraph's ainvoke returns the merged state dict. When an
    interrupt has occurred, the checkpointer's latest state will
    show the graph waiting at the human_review node; the simplest
    heuristic is: `flagged_updates` is a non-empty list AND there is
    no `final_json` yet. That is exactly the pause point for our
    graph, and the graph cannot reach `final_json` without passing
    through merge.
    """
    if not isinstance(result, dict):
        return False, []
    flagged = result.get("flagged_updates") or []
    final_json = result.get("final_json")
    if (
        isinstance(flagged, list)
        and flagged
        and not isinstance(final_json, dict)
    ):
        return True, flagged
    return False, []


async def _process_one(msg: SQSMessage) -> None:
    """
    Run the pipeline for a single parsed message. Raises on transient
    failure so the caller can decide whether to delete (success /
    poison) or leave for re-drive (transient).

    Two message flavours are handled:

      * Fresh run (`resume=False`): build an initial `PipelineState` via
        `new_pipeline_state(...)` and invoke the graph from START. Can
        pause at human_review → status becomes 'waiting_review'.
      * Resume (`resume=True`): the graph is already suspended at
        human_review for this run_id; we invoke it with
        `Command(resume={"decisions": [...]})` to feed the caregiver's
        decisions in. The reassessment graph has exactly one interrupt
        point, so a resumed run always reaches a terminal state in this
        worker invocation.
    """
    graph = get_graph_for(msg.pipeline_type)

    # Flip queued → running. For fresh runs, this records started_at; for
    # resumes, started_at was already set on the initial invocation and
    # stays intact (see `_mark_run_running`). A duplicate SQS delivery of
    # either flavour hits the terminal-state guard and bails out.
    await _mark_run_running(msg)

    if msg.resume:
        # Resume payload must match the shape expected by human_review_node
        # (see `_coerce_decisions` in that module). Serialising each
        # ReviewDecision to a plain dict keeps the wire shape identical to
        # what the old synchronous /review path handed the graph.
        resume_payload = {
            "decisions": [d.model_dump() for d in (msg.decisions or [])],
        }
        invoke_arg: Any = Command(resume=resume_payload)
    else:
        invoke_arg = new_pipeline_state(
            run_id=msg.run_id,
            patient_id=msg.patient_id,
            user_id=msg.user_id,
            pipeline_type=msg.pipeline_type,
            pdf_s3_key=msg.s3_key if msg.pipeline_type == PIPELINE_INTAKE else None,
            audio_s3_key=msg.s3_key if msg.pipeline_type == PIPELINE_REASSESSMENT else None,
        )

    result = await graph.ainvoke(invoke_arg, config=run_config(msg.run_id))

    # Only a fresh run can legitimately pause. A resume that returns with
    # flagged_updates set and no final_json indicates a graph-topology
    # change (a second interrupt point was added); rather than silently
    # parking the run back in waiting_review, we fall through to the
    # terminal path and let missing final_json surface as a completion
    # without an output PDF — operators will see it in logs.
    if not msg.resume:
        paused, flagged = _has_interrupt(result)
        if paused:
            await _mark_run_waiting_review(run_id=msg.run_id, flagged=flagged)
            await notify_review_required(
                run_id=msg.run_id, patient_id=msg.patient_id
            )
            logger.info(
                "run paused for review: %s",
                summarize_state(result),
            )
            return

    output_pdf_s3_key = result.get("output_pdf_s3_key") if isinstance(result, dict) else None
    await _mark_run_complete(
        run_id=msg.run_id,
        output_pdf_s3_key=output_pdf_s3_key,
    )
    await notify_pipeline_complete(
        run_id=msg.run_id, patient_id=msg.patient_id
    )
    logger.info(
        "run complete%s: %s",
        " (resumed)" if msg.resume else "",
        summarize_state(result if isinstance(result, dict) else {}),
    )


async def _handle_message(
    *,
    message: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> None:
    """
    Full per-message lifecycle: parse → process → delete/fail.
    Runs under a concurrency semaphore.
    """
    message_id = message.get("MessageId") or "<unknown>"
    receipt = message.get("ReceiptHandle")
    body = message.get("Body")

    if not isinstance(receipt, str) or not receipt:
        logger.warning(
            "sqs message missing receipt handle: id=%s", message_id
        )
        return

    async with semaphore:
        # --- parse ------------------------------------------------------- #
        try:
            parsed = _parse_body(body)
        except ValueError as exc:
            # Poison — delete so the queue doesn't churn forever.
            logger.warning(
                "sqs poison message dropped: id=%s err=%s",
                message_id,
                type(exc).__name__,
            )
            await _delete_message(receipt)
            return

        # --- run --------------------------------------------------------- #
        try:
            await _process_one(parsed)
        except RuntimeError as exc:
            # Special: "run already terminal" — duplicate delivery,
            # safe to delete.
            if "already terminal" in str(exc):
                logger.info(
                    "sqs duplicate delivery dropped: run_id=%s",
                    parsed.run_id,
                )
                await _delete_message(receipt)
                return
            # Fall through to generic failure handling.
            await _handle_failure(parsed, exc)
        except (ClientError, BotoCoreError) as exc:
            # Transport failures to AWS. Treat as transient — don't
            # mark the run failed; let SQS re-deliver after the
            # visibility timeout.
            logger.warning(
                "transient AWS error for run_id=%s: %s — will redeliver",
                parsed.run_id,
                type(exc).__name__,
            )
            return
        except Exception as exc:  # noqa: BLE001
            await _handle_failure(parsed, exc)
            # _handle_failure already marked run failed and notified;
            # delete the message so SQS does not re-deliver the now-
            # terminal run.
            await _delete_message(receipt)
            return
        else:
            await _delete_message(receipt)
            return


async def _handle_failure(parsed: SQSMessage, exc: BaseException) -> None:
    """
    Common failure path: mark run failed with a bounded opaque message,
    fire the failure notification, log the error TYPE (never str(exc)).
    """
    error_text = f"{type(exc).__name__}"
    try:
        await _mark_run_failed(run_id=parsed.run_id, error_text=error_text)
    except Exception as db_exc:  # noqa: BLE001
        logger.error(
            "failed to mark run failed: run_id=%s err=%s",
            parsed.run_id,
            type(db_exc).__name__,
        )
    try:
        await notify_pipeline_failed(
            run_id=parsed.run_id, patient_id=parsed.patient_id
        )
    except Exception as push_exc:  # noqa: BLE001
        logger.warning(
            "failure notification error: run_id=%s err=%s",
            parsed.run_id,
            type(push_exc).__name__,
        )
    logger.error(
        "run failed: run_id=%s err=%s",
        parsed.run_id,
        type(exc).__name__,
    )


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
async def poll_loop(stop_event: asyncio.Event) -> None:
    """
    Long-poll SQS until `stop_event` is set. Each received message is
    handed to `_handle_message` under a concurrency semaphore.
    """
    settings = get_settings()
    semaphore = asyncio.Semaphore(settings.WORKER_CONCURRENCY)
    in_flight: set[asyncio.Task[None]] = set()

    logger.info(
        "worker poll loop started: concurrency=%d wait=%ds visibility=%ds",
        settings.WORKER_CONCURRENCY,
        settings.SQS_WAIT_TIME_SECONDS,
        settings.SQS_VISIBILITY_TIMEOUT,
    )

    while not stop_event.is_set():
        try:
            messages = await _receive_messages(
                max_messages=settings.SQS_MAX_MESSAGES_PER_POLL
            )
        except (ClientError, BotoCoreError) as exc:
            logger.warning(
                "sqs receive error: %s; backing off",
                type(exc).__name__,
            )
            await asyncio.sleep(_IDLE_BACKOFF_SECONDS)
            continue

        if not messages:
            # Empty poll — loop again immediately; long-polling already
            # absorbed the wait.
            continue

        for message in messages:
            if stop_event.is_set():
                break
            task = asyncio.create_task(
                _handle_message(message=message, semaphore=semaphore)
            )
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)

    if in_flight:
        logger.info(
            "worker draining %d in-flight tasks before shutdown",
            len(in_flight),
        )
        await asyncio.gather(*in_flight, return_exceptions=True)

    logger.info("worker poll loop stopped")


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """
    Install SIGTERM / SIGINT → stop_event.set(). On Windows, signal
    hookup is limited; we use `add_signal_handler` when available and
    fall back to the plain signal module otherwise.
    """
    def _handler(*_args: Any) -> None:
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            # Windows asyncio loops don't support add_signal_handler.
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not in the main thread, or signal unavailable.
                pass


async def main() -> None:
    """
    Async entry point: configure, init, run the poll loop, tear down.
    """
    settings = get_settings()
    configure_logging(settings)
    settings.enforce_production_requirements()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop_event)

    await init_db()
    await init_pipeline()

    try:
        await poll_loop(stop_event)
    finally:
        await shutdown_pipeline()
        await shutdown_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


__all__ = ["main", "poll_loop"]
