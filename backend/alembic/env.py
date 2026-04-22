"""
Alembic environment for the Samni Labs backend.

The connection URL is pulled from `config.Settings.DATABASE_URL_SYNC` at
runtime — never from alembic.ini. This keeps database credentials out of
version control and guarantees migrations target the same database the
application is configured for.

Security posture for migrations:
  * Sync driver (psycopg2) is used because Alembic itself is synchronous;
    TLS is enforced via `sslmode=require` for any non-dev environment,
    matching the async engine's policy in database.py.
  * A 5-minute `statement_timeout` is set on the migration session so a
    runaway or hung DDL can't block the team indefinitely. This is longer
    than the app's 30-second cap because real DDL (index builds on large
    tables) is legitimately slower than OLTP queries.
  * `lock_timeout=10s` prevents a migration from queueing forever behind
    a held row lock — better to fail loudly and retry in a window.
  * `transaction_per_migration=True` means each revision runs in its own
    transaction; a failed revision rolls back cleanly instead of leaving
    half the schema ahead of the version table.
  * `compare_type=True` and `compare_server_default=True` catch silent
    drift between models and the live database on autogenerate. Without
    these, a column type change slips through and produces a migration
    that doesn't match the model.
  * DSN, credentials, and bound parameters are NEVER logged. Only the
    hostname is included in the startup log line.
"""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import urlparse

from alembic import context
from sqlalchemy import engine_from_config, pool


# --------------------------------------------------------------------------- #
# Make the backend/ directory importable regardless of invocation cwd.
# --------------------------------------------------------------------------- #
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# These imports must come AFTER the sys.path tweak above. They are not top-of-
# file style-violations — they depend on the path being set.
from config import get_settings  # noqa: E402
from database import Base  # noqa: E402

# Importing db_models registers every ORM table against `Base.metadata`.
# Without this side-effect import, `alembic revision --autogenerate` would
# find an empty metadata object and produce a no-op migration.
import db_models  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# Alembic config + logging
# --------------------------------------------------------------------------- #
config = context.config

# Configure Python logging from the [loggers] section of alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")


# --------------------------------------------------------------------------- #
# Inject DSN from application settings (NOT from alembic.ini)
# --------------------------------------------------------------------------- #
_settings = get_settings()
if not _settings.DATABASE_URL_SYNC:
    raise RuntimeError(
        "DATABASE_URL_SYNC is empty; Alembic cannot run without a sync "
        "PostgreSQL DSN. Set DATABASE_URL_SYNC in the environment."
    )

# `set_main_option` escapes % correctly for configparser before the value
# reaches engine_from_config; passing it through Alembic is safer than
# concatenating ourselves.
config.set_main_option("sqlalchemy.url", _settings.DATABASE_URL_SYNC)

# Target metadata for autogenerate. `db_models` has already populated this.
target_metadata = Base.metadata


def _log_safe_host() -> str:
    """Return just the hostname of the sync DSN for log lines (never password)."""
    try:
        return urlparse(_settings.DATABASE_URL_SYNC).hostname or "unknown"
    except Exception:  # noqa: BLE001 — logging helper must not raise
        return "unknown"


def _sync_connect_args() -> dict[str, object]:
    """Build psycopg2 connect_args with the same hardening as database.py."""
    # `options` is a psycopg2 pass-through for libpq `-c` GUC overrides.
    # Migrations need a longer statement_timeout than OLTP (index builds,
    # table rewrites) but keep lock_timeout short so a blocked migration
    # fails fast rather than stalling the team.
    args: dict[str, object] = {
        "options": "-c statement_timeout=300000 -c lock_timeout=10000",
    }

    # TLS is mandatory for RDS; local dev over a loopback socket can skip it.
    if not _settings.is_dev:
        args["sslmode"] = "require"

    return args


# --------------------------------------------------------------------------- #
# Shared Alembic context options
# --------------------------------------------------------------------------- #
def _context_kwargs() -> dict[str, object]:
    """Keyword args passed to `context.configure()` in both modes."""
    return {
        "target_metadata": target_metadata,
        # Catch type drift between the model and the live schema.
        "compare_type": True,
        "compare_server_default": True,
        # We use only the default `public` schema; skip cross-schema scans.
        "include_schemas": False,
        # `render_as_batch` is for SQLite's broken ALTER TABLE; we're on
        # PostgreSQL and want native ALTERs.
        "render_as_batch": False,
        # Each revision runs in its own transaction.
        "transaction_per_migration": True,
    }


# --------------------------------------------------------------------------- #
# Offline mode — emit SQL to stdout without connecting
# --------------------------------------------------------------------------- #
def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Configures the context with the URL only (no Engine). Emits SQL to
    stdout so a reviewer can vet the exact statements before they're
    applied to a real database. Useful for production change-management
    workflows where DDL must be peer-reviewed.
    """
    url = config.get_main_option("sqlalchemy.url")
    logger.info(
        "Alembic offline migrations starting host=%s env=%s",
        _log_safe_host(),
        _settings.ENVIRONMENT,
    )

    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_context_kwargs(),
    )

    with context.begin_transaction():
        context.run_migrations()

    logger.info("Alembic offline migrations complete")


# --------------------------------------------------------------------------- #
# Online mode — connect and apply
# --------------------------------------------------------------------------- #
def run_migrations_online() -> None:
    """
    Run migrations against a live database connection.

    Uses `NullPool` so the migration process opens a single connection,
    applies the revisions, and releases it. No pooling is needed for a
    one-shot migration run, and NullPool avoids keeping stale connections
    open if Alembic is embedded in a longer-running script.
    """
    logger.info(
        "Alembic online migrations starting host=%s env=%s",
        _log_safe_host(),
        _settings.ENVIRONMENT,
    )

    section = config.get_section(config.config_ini_section, {}) or {}

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_sync_connect_args(),
    )

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                **_context_kwargs(),
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        # Dispose even on failure so we never leak the single connection
        # back to the OS's TIME_WAIT pile for longer than necessary.
        connectable.dispose()

    logger.info("Alembic online migrations complete")


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
