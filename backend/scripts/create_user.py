"""
CLI: create a user in the backend's `users` table.

Usage:
    python -m scripts.create_user --username cynthia --password "her-password"

Optional flags:
    --id <uuid>          override the generated user id
    --replace            if a user with this username already exists,
                         REPLACE the password_hash on the existing row
                         instead of refusing.

Why this exists:
    The backend has no public sign-up endpoint (deliberately — single
    tenant). The very first user has to be created out-of-band. Run
    this once on the server (or once locally for dev), then the user
    can log in via POST /auth/login.

Security:
    * The password is read from the command line by default. On a
      shared shell that is visible in `ps`. To avoid that, omit
      `--password` and the script will prompt interactively (input
      is masked).
    * The plain password is hashed with bcrypt BEFORE any DB write.
      We never store, log, or echo the plain password.
    * The script refuses to run with `ENVIRONMENT=production` UNLESS
      `--allow-prod` is also passed — accidental prod user creation
      from a developer laptop is a real risk worth a guard rail.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

# Allow running with `python scripts/create_user.py` as well as
# `python -m scripts.create_user`. The path tweak below is best-effort
# and harmless when running as a module.
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from auth import hash_password  # noqa: E402
from config import configure_logging, get_settings  # noqa: E402
from database import get_db_session, init_db, shutdown_db  # noqa: E402
from db_models import User  # noqa: E402

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="create_user",
        description="Create (or replace) a user in the Samni Labs backend.",
    )
    p.add_argument(
        "--username",
        required=True,
        help="Username for the new user (case-sensitive, max 64 chars).",
    )
    p.add_argument(
        "--password",
        default=None,
        help=(
            "Password. If omitted, the script prompts for it interactively "
            "(recommended — keeps the plaintext out of `ps` and shell history)."
        ),
    )
    p.add_argument(
        "--id",
        default=None,
        help="Override the generated user id (UUID-shaped string up to 64 chars).",
    )
    p.add_argument(
        "--replace",
        action="store_true",
        help=(
            "If a user with this username already exists, replace the "
            "password_hash on the existing row instead of refusing."
        ),
    )
    p.add_argument(
        "--allow-prod",
        action="store_true",
        help="Required to run with ENVIRONMENT=production.",
    )
    return p.parse_args()


def _resolve_password(arg_value: Optional[str]) -> str:
    if arg_value is not None:
        return arg_value
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Re-enter password: ")
    if pw1 != pw2:
        print("Passwords do not match. Aborting.", file=sys.stderr)
        sys.exit(2)
    return pw1


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.is_production and not args.allow_prod:
        print(
            "Refusing to create users in production without --allow-prod.",
            file=sys.stderr,
        )
        return 2

    if not 1 <= len(args.username) <= 64:
        print("--username must be 1..64 characters.", file=sys.stderr)
        return 2

    password = _resolve_password(args.password)
    if not 1 <= len(password) <= 1024:
        print("--password must be 1..1024 characters.", file=sys.stderr)
        return 2

    pw_hash = hash_password(password)

    user_id = args.id or str(uuid.uuid4())
    if not 1 <= len(user_id) <= 64:
        print("--id must be 1..64 characters.", file=sys.stderr)
        return 2

    await init_db()
    try:
        async with get_db_session() as session:
            existing = await session.scalar(
                select(User).where(User.username == args.username)
            )
            if existing is not None and not args.replace:
                print(
                    f"User with username {args.username!r} already exists. "
                    f"Pass --replace to overwrite the password_hash.",
                    file=sys.stderr,
                )
                return 1

            try:
                if existing is not None:
                    existing.password_hash = pw_hash
                    action = "replaced password for"
                    user_id = existing.id
                else:
                    session.add(
                        User(
                            id=user_id,
                            username=args.username,
                            password_hash=pw_hash,
                        )
                    )
                    action = "created"
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                print(
                    f"Database error: {type(exc).__name__}",
                    file=sys.stderr,
                )
                return 1
    finally:
        await shutdown_db()

    print(f"OK — {action} user id={user_id} username={args.username}")
    return 0


def main() -> None:
    args = _parse_args()
    configure_logging(get_settings())
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
