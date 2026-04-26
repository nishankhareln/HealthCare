"""
LangGraph pipeline construction + checkpointer wiring.

Two graphs:

    INTAKE          START → parse_pdf → save_json → audit → END
    INTAKE_DOCTOR   START → parse_doctor_pdf → save_json → audit → END
    REASSESSMENT    START → load_patient_json → transcribe → llm_map →
                     llm_critic → confidence → human_review → merge →
                     save_json → audit → END

    Audio handling: Flutter records the dictation on-device and uploads
    the audio file to S3 (`audio/` prefix). The `transcribe` node calls
    Amazon Transcribe Medical's async batch API against that S3 object
    and stores the resulting transcript at `transcripts/{run_id}.json`.
    The transcript text is then placed on `state.transcript` for
    `llm_map` to consume.

    PDF rendering is NOT a backend step. The Flutter app holds the fixed
    WAC-388-76-615 Negotiated Care Plan template, renders it locally
    from the patient JSON, and uploads the signed PDF back to S3 via
    POST /signed-pdf/{run_id}.

Both graphs share a single PostgreSQL checkpointer (AsyncPostgresSaver
from `langgraph-checkpoint-postgres`). The checkpointer persists every
state transition to the same Postgres instance that holds patient
records. Run identity = UUID; we use `{"configurable": {"thread_id": run_id}}`
to key checkpoints.

Public API:
    init_pipeline()        — one-time setup: connect, create checkpoint
                             tables, compile graphs. Call at app startup.
    shutdown_pipeline()    — close the checkpointer connection. Call at
                             shutdown.
    get_intake_graph()     — return the compiled intake graph.
    get_reassessment_graph()
                           — return the compiled reassessment graph.
    run_config(run_id)     — build the LangGraph config with thread_id
                             set to run_id.

Security posture:
  * The checkpointer uses the same DSN as the main DB (converted from
    SQLAlchemy's `+asyncpg` form to plain postgres:// for psycopg,
    which is what langgraph-checkpoint-postgres uses).
  * In non-dev environments `sslmode=require` is appended to the DSN
    if missing, matching `database.py`. Checkpoints carry PHI and must
    ride TLS.
  * Connections are pooled through an `AsyncConnectionPool` with
    `min_size=1`, `max_size=WORKER_CONCURRENCY*2`, and `max_idle=300`.
    This prevents a runaway pipeline from opening a connection per
    run.
  * Init is ONCE-ONLY and idempotent. Concurrent callers are serialised
    by an asyncio.Lock. If init fails we leave the module in an
    uninitialised state so a retry is safe.
  * We NEVER log state contents. Use `state.summarize_state` if a
    caller wants to log run progress.
  * All nodes are registered with the graph; their own PHI-safety
    posture is documented in their own modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, Optional
from urllib.parse import urlparse, urlunparse

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg_pool import AsyncConnectionPool

from config import get_settings
from db_models import (
    PIPELINE_TYPE_INTAKE,
    PIPELINE_TYPE_INTAKE_DOCTOR,
    PIPELINE_TYPE_REASSESSMENT,
)
from nodes.audit import audit_node
from nodes.confidence import confidence_node
from nodes.human_review import human_review_node
from nodes.llm_critic import llm_critic_node
from nodes.llm_map import llm_map_node
from nodes.load_patient_json import load_patient_json_node
from nodes.merge import merge_node
from nodes.parse_doctor_pdf import parse_doctor_pdf_node
from nodes.parse_pdf import parse_pdf_node
from nodes.save_json import save_json_node
from nodes.transcribe import transcribe_node
from state import PipelineState

logger = logging.getLogger(__name__)



# Module state (guarded by an asyncio.Lock)

_init_lock: Final[asyncio.Lock] = asyncio.Lock()
_pool: Optional[AsyncConnectionPool] = None
_checkpointer: Optional[AsyncPostgresSaver] = None
_intake_graph: Optional[Any] = None
_doctor_intake_graph: Optional[Any] = None
_reassessment_graph: Optional[Any] = None



# DSN conversion for the checkpointer (psycopg, not asyncpg)

def _to_psycopg_dsn(sqlalchemy_dsn: str) -> str:
    """
    Convert `postgresql+asyncpg://...` (SQLAlchemy) to a plain
    `postgresql://...` DSN suitable for psycopg3. Appends
    `sslmode=require` in non-dev environments if no sslmode is set.
    """
    parsed = urlparse(sqlalchemy_dsn)
    scheme = "postgresql"
    # Replace the scheme while preserving everything else.
    rebuilt = urlunparse(
        (
            scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )

    settings = get_settings()
    if not settings.is_dev and "sslmode=" not in (parsed.query or ""):
        sep = "&" if parsed.query else "?"
        rebuilt = f"{rebuilt}{sep}sslmode=require"
    return rebuilt


def _safe_dsn_host(dsn: str) -> str:
    """
    Return just the host portion of a DSN, never the full string —
    full DSNs contain credentials.
    """
    try:
        return urlparse(dsn).hostname or "<unknown>"
    except Exception:  # noqa: BLE001 — parser never raises but we stay defensive
        return "<unknown>"



# Graph construction (pure, no side effects)

def _route_after_confidence(state: PipelineState) -> str:
    """
    Conditional edge: after confidence routing, send through
    human_review if there are flagged updates, else straight to merge.

    The human_review node is a safe no-op when flagged is empty, so
    routing always through it would also work — but skipping the node
    entirely avoids an unnecessary checkpoint round-trip on the happy
    path where every update is auto-approved.
    """
    flagged = state.get("flagged_updates") or []
    if isinstance(flagged, list) and flagged:
        return "human_review"
    return "merge"


def _build_intake_graph() -> StateGraph:
    """
    Build the INTAKE graph (caregiver-uploaded 36-page DSHS Assessment
    Details PDF).

        parse_pdf → save_json → audit → END

    Owns demographics, mobility, ADLs, behaviors, social — the
    non-medical portion of the assessment.

    PDF generation is deliberately NOT a backend step. The Flutter app holds
    the WAC-388-76-615 Negotiated Care Plan template, renders it locally
    from the patient JSON, captures the caregiver's signature, and uploads
    the signed PDF back to S3 (see POST /signed-pdf/{run_id}).
    """
    graph = StateGraph(PipelineState)
    graph.add_node("parse_pdf", parse_pdf_node)
    graph.add_node("save_json", save_json_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "parse_pdf")
    graph.add_edge("parse_pdf", "save_json")
    graph.add_edge("save_json", "audit")
    graph.add_edge("audit", END)

    return graph


def _build_doctor_intake_graph() -> StateGraph:
    """
    Build the DOCTOR INTAKE graph (doctor-supplied ~15-page PDF that
    covers diseases, diagnoses, medications, conditions).

        parse_doctor_pdf → save_json → audit → END

    Same shape as the regular intake graph, but the parse node runs a
    doctor-PDF-specific Bedrock prompt that extracts ONLY the medical
    sections of the assessment and merges them into the existing
    patient JSON. Caregiver-owned fields (mobility, ADLs, etc.) are
    preserved verbatim.
    """
    graph = StateGraph(PipelineState)
    graph.add_node("parse_doctor_pdf", parse_doctor_pdf_node)
    graph.add_node("save_json", save_json_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "parse_doctor_pdf")
    graph.add_edge("parse_doctor_pdf", "save_json")
    graph.add_edge("save_json", "audit")
    graph.add_edge("audit", END)

    return graph


def _build_reassessment_graph() -> StateGraph:
    """
    Build the REASSESSMENT graph.

        load_patient_json → transcribe → llm_map → llm_critic →
        confidence → (human_review?) → merge → save_json → audit → END

    Transcription runs on the backend via Amazon Transcribe Medical's
    async batch API. Flutter uploads the audio file to S3, the worker
    invokes this graph, and the `transcribe` node turns that audio into
    text inside `state.transcript`.

    As with intake, the backend does not render the final PDF — Flutter
    renders it from the merged JSON and uploads the signed artifact back.
    """
    graph = StateGraph(PipelineState)
    graph.add_node("load_patient_json", load_patient_json_node)
    graph.add_node("transcribe", transcribe_node)
    graph.add_node("llm_map", llm_map_node)
    graph.add_node("llm_critic", llm_critic_node)
    graph.add_node("confidence", confidence_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("merge", merge_node)
    graph.add_node("save_json", save_json_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "load_patient_json")
    graph.add_edge("load_patient_json", "transcribe")
    graph.add_edge("transcribe", "llm_map")
    graph.add_edge("llm_map", "llm_critic")
    graph.add_edge("llm_critic", "confidence")
    graph.add_conditional_edges(
        "confidence",
        _route_after_confidence,
        {
            "human_review": "human_review",
            "merge": "merge",
        },
    )
    graph.add_edge("human_review", "merge")
    graph.add_edge("merge", "save_json")
    graph.add_edge("save_json", "audit")
    graph.add_edge("audit", END)

    return graph



# Lifecycle

async def init_pipeline() -> None:
    """
    One-time pipeline setup: open the checkpoint-DB connection pool,
    set up the AsyncPostgresSaver tables, compile both graphs.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    global _pool, _checkpointer, _intake_graph, _doctor_intake_graph, _reassessment_graph

    async with _init_lock:
        if (
            _intake_graph is not None
            and _doctor_intake_graph is not None
            and _reassessment_graph is not None
        ):
            return

        settings = get_settings()
        dsn = _to_psycopg_dsn(settings.DATABASE_URL)

        # Pool sizing: one connection per worker job, doubled for
        # saver/setup overlap. Clamped to avoid exhausting DB slots.
        max_pool = max(2, settings.WORKER_CONCURRENCY * 2)

        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=max_pool,
            max_idle=300.0,
            kwargs={"autocommit": True},
            # Do not open eagerly; we call `open()` to get a clear
            # error surface and to match langgraph's expected lifecycle.
            open=False,
        )

        try:
            await pool.open()
            checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
            # Idempotent migration — safe to call every boot.
            await checkpointer.setup()
        except Exception:
            # Ensure we don't leak the pool if setup fails partway.
            try:
                await pool.close()
            except Exception:  # noqa: BLE001
                pass
            raise

        _pool = pool
        _checkpointer = checkpointer
        _intake_graph = _build_intake_graph().compile(checkpointer=checkpointer)
        _doctor_intake_graph = _build_doctor_intake_graph().compile(
            checkpointer=checkpointer
        )
        _reassessment_graph = _build_reassessment_graph().compile(
            checkpointer=checkpointer
        )

        logger.info(
            "pipeline initialized: host=%s max_pool=%d",
            _safe_dsn_host(dsn),
            max_pool,
        )


async def shutdown_pipeline() -> None:
    """
    Close the checkpointer pool. Safe to call multiple times.
    """
    global _pool, _checkpointer, _intake_graph, _doctor_intake_graph, _reassessment_graph

    async with _init_lock:
        pool = _pool
        _pool = None
        _checkpointer = None
        _intake_graph = None
        _doctor_intake_graph = None
        _reassessment_graph = None

    if pool is None:
        return

    try:
        await pool.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline shutdown close error: %s", type(exc).__name__)
        return

    logger.info("pipeline shut down")


def _require_initialised() -> None:
    if (
        _intake_graph is None
        or _doctor_intake_graph is None
        or _reassessment_graph is None
    ):
        raise RuntimeError(
            "pipeline not initialised; call init_pipeline() at startup"
        )


def get_intake_graph() -> Any:
    """Return the compiled intake graph. Raises if init was not called."""
    _require_initialised()
    return _intake_graph


def get_doctor_intake_graph() -> Any:
    """Return the compiled doctor-intake graph. Raises if init was not called."""
    _require_initialised()
    return _doctor_intake_graph


def get_reassessment_graph() -> Any:
    """Return the compiled reassessment graph. Raises if init was not called."""
    _require_initialised()
    return _reassessment_graph


def get_graph_for(pipeline_type: str) -> Any:
    """Dispatch by `pipeline_type` from state/db."""
    if pipeline_type == PIPELINE_TYPE_INTAKE:
        return get_intake_graph()
    if pipeline_type == PIPELINE_TYPE_INTAKE_DOCTOR:
        return get_doctor_intake_graph()
    if pipeline_type == PIPELINE_TYPE_REASSESSMENT:
        return get_reassessment_graph()
    raise ValueError(f"unknown pipeline_type: {pipeline_type!r}")


# Config builder

def run_config(run_id: str) -> dict[str, Any]:
    """
    Build the LangGraph config dict keyed by `run_id`. LangGraph
    persists checkpoints per `thread_id`; we use the run_id so resumes
    from the /review endpoint land on the same checkpoint as the
    original invocation.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    return {"configurable": {"thread_id": run_id}}


__all__ = [
    "init_pipeline",
    "shutdown_pipeline",
    "get_intake_graph",
    "get_doctor_intake_graph",
    "get_reassessment_graph",
    "get_graph_for",
    "run_config",
]
