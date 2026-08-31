"""SQLite specifics that appkit_commons' single database URL cannot express.

``appkit_commons`` exposes exactly one :attr:`DatabaseConfig.url` and hands it
to both the sync and the async engine. Only an async driver can serve the
application itself — every page state and the job queue open an
``AsyncSession`` — so the configured URL names ``aiosqlite`` and the sync-only
consumers (Alembic and Reflex's ``db_url``) ask :func:`sync_database_url` for
the pysqlite variant.

SQLite also needs three connection settings that PostgreSQL gave for free:
write-ahead logging so a reader is not blocked by a writer, enforced foreign
keys (without them SQLite silently ignores ``ON DELETE CASCADE``), and a busy
timeout so a second writer waits instead of raising ``database is locked``.
"""

import logging
from pathlib import Path
from typing import Any

from appkit_commons.database.configuration import DatabaseConfig
from sqlalchemy import event, make_url
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

ASYNC_DRIVER = "+aiosqlite"
"""The driver token that makes the configured URL usable by the async engine."""

BUSY_TIMEOUT_MS = 5000
"""How long a blocked writer waits for the lock before giving up."""

_pragmas_installed = False


def prepare(database: DatabaseConfig | None) -> None:
    """Make the configured SQLite file usable before anything connects to it.

    A no-op for any other database, so the caller needs no ``if``.
    """
    if database is None or database.type != "sqlite":
        return
    ensure_database_directory(database.url)
    install_pragmas()


def sync_database_url(url: str) -> str:
    """Return ``url`` with the async driver stripped, for blocking consumers."""
    return url.replace(ASYNC_DRIVER, "", 1)


def database_path(url: str) -> Path | None:
    """Return the file ``url`` is backed by, or ``None`` if it is in memory."""
    database = make_url(url).database
    if not database or database == ":memory:":
        return None
    return Path(database)


def ensure_database_directory(url: str) -> None:
    """Create the directory holding the SQLite file, so the driver can open it.

    The configured location lives under ``.state/``, which is gitignored and
    wiped by ``task clean``, so it is routinely missing on a fresh checkout.
    """
    path = database_path(url)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # The database holds accounts and encrypted credentials — private mail
    # data. An explicit chmod rather than a mkdir mode, so neither a permissive
    # umask nor a directory an earlier version already created can leave it
    # readable to other users.
    path.parent.chmod(0o700)
    logger.debug("SQLite database directory ready: %s", path.parent)


def install_pragmas() -> None:
    """Apply the SQLite settings to every engine this process opens.

    Registered on the ``Engine`` class rather than on one engine because
    appkit_commons builds the sync and the async engine lazily behind its own
    caches, and Reflex builds a third — all of them against the single SQLite
    file this application owns. Calling this more than once is a no-op.
    """
    global _pragmas_installed
    if _pragmas_installed:
        return
    event.listen(Engine, "connect", _apply_pragmas)
    _pragmas_installed = True
    logger.debug("SQLite pragmas will be applied to new connections")


def _apply_pragmas(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ARG001
    """Set the per-connection pragmas; SQLite keeps none of them across connects."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()
