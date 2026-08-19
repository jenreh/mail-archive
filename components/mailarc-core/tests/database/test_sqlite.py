"""Tests for the SQLite wiring in :mod:`mailarc_core.database.sqlite`."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from sqlalchemy import create_engine, text

from mailarc_core.database import sqlite


@pytest.fixture(autouse=True)
def _reset_pragma_installation() -> Iterator[None]:
    """Keep the module-level listener flag from leaking between tests."""
    before = sqlite._pragmas_installed
    yield
    sqlite._pragmas_installed = before


class TestSyncDatabaseUrl:
    def test_strips_the_async_driver(self) -> None:
        assert (
            sqlite.sync_database_url("sqlite+aiosqlite:///.state/mail-archive.db")
            == "sqlite:///.state/mail-archive.db"
        )

    def test_leaves_an_already_blocking_url_alone(self) -> None:
        url = "sqlite:///.state/mail-archive.db"
        assert sqlite.sync_database_url(url) == url


class TestDatabasePath:
    def test_returns_the_file_a_url_points_at(self) -> None:
        path = sqlite.database_path("sqlite+aiosqlite:///.state/mail-archive.db")
        assert path == Path(".state/mail-archive.db")

    @pytest.mark.parametrize(
        "url",
        ["sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:"],
    )
    def test_returns_none_for_an_in_memory_database(self, url) -> None:
        assert sqlite.database_path(url) is None


class TestEnsureDatabaseDirectory:
    def test_creates_the_missing_parent_directory(self, tmp_path) -> None:
        target = tmp_path / "state" / "mail-archive.db"

        sqlite.ensure_database_directory(f"sqlite+aiosqlite:///{target}")

        assert target.parent.is_dir()

    def test_is_a_noop_for_an_in_memory_database(self) -> None:
        sqlite.ensure_database_directory("sqlite+aiosqlite:///:memory:")


class TestInstallPragmas:
    def test_new_connections_get_wal_foreign_keys_and_a_busy_timeout(
        self, tmp_path
    ) -> None:
        sqlite._pragmas_installed = False
        sqlite.install_pragmas()

        engine = create_engine(f"sqlite:///{tmp_path / 'pragmas.db'}")
        try:
            with engine.connect() as connection:
                journal_mode = connection.execute(
                    text("PRAGMA journal_mode")
                ).scalar_one()
                foreign_keys = connection.execute(
                    text("PRAGMA foreign_keys")
                ).scalar_one()
                busy_timeout = connection.execute(
                    text("PRAGMA busy_timeout")
                ).scalar_one()
        finally:
            engine.dispose()

        assert journal_mode == "wal"
        assert foreign_keys == 1
        assert busy_timeout == sqlite.BUSY_TIMEOUT_MS

    def test_registers_the_listener_only_once(self, monkeypatch) -> None:
        registrations: list[str] = []
        monkeypatch.setattr(
            sqlite.event,
            "listen",
            lambda *args, **kwargs: registrations.append("listen"),
        )
        sqlite._pragmas_installed = False

        sqlite.install_pragmas()
        sqlite.install_pragmas()

        assert registrations == ["listen"]


class TestPrepare:
    """`prepare` decides whether the SQLite setup applies at all."""

    def test_prepares_a_sqlite_database(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "state" / "mail-archive.db"
        calls: list[str] = []
        monkeypatch.setattr(
            sqlite,
            "ensure_database_directory",
            lambda url: calls.append(f"dir:{url}"),
        )
        monkeypatch.setattr(sqlite, "install_pragmas", lambda: calls.append("pragmas"))

        sqlite.prepare(
            DatabaseConfig.model_validate(
                {"type": "sqlite", "url": f"sqlite+aiosqlite:///{target}"}
            )
        )

        assert calls == [f"dir:sqlite+aiosqlite:///{target}", "pragmas"]

    @pytest.mark.parametrize(
        "database",
        [None, DatabaseConfig(type="postgresql")],
    )
    def test_leaves_anything_that_is_not_sqlite_alone(
        self, database, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            sqlite,
            "ensure_database_directory",
            lambda url: pytest.fail("must not touch the filesystem"),
        )
        monkeypatch.setattr(
            sqlite,
            "install_pragmas",
            lambda: pytest.fail("must not register a listener"),
        )

        sqlite.prepare(database)
