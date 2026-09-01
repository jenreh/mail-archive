"""Tests for :class:`mailarc_sync.erase.eraser.AccountEraser`.

A real SQLite file for the ledgers and a recording session for the graph. The
split is deliberate: what the graph half deletes is
``mailarc_core.archive.purge``'s claim and is proved twice over there, once
against a fake session and once against a vendored FalkorDB. What is left for
this module — and therefore for this file — is the *order* the two stores are
touched in, the refusal when a job is open, and the fact that the mailbox
itself survives.

The session factory mirrors appkit's ``AsyncSessionManager``: commit on a clean
exit, roll back on an exception. That is the contract the eraser assumes of
``get_asyncdb_session`` in production.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailarc_core.database.entities import (
    MailAccountEntity,
    MailSyncJobEntity,
    SyncJobKind,
    SyncJobState,
)
from mailarc_core.database.repositories import (
    ArchivedMessageRepository,
    FailedMessageRepository,
    SyncCheckpointRepository,
)
from mailarc_sync.erase import AccountBusy, AccountEraser, EraseCounts

type SessionFactory = Any


class RecordingGraph:
    """A graph session that answers the purge's statements out of a dict.

    Deliberately the smallest thing that lets the purge run to completion: it
    hands back one page of ids, reports none of them shared, and counts the
    delete. What the statements *say* is pinned in ``mailarc-core``; what this
    file needs is that the graph half ran, and when.
    """

    def __init__(self, message_ids: list[str]) -> None:
        self.message_ids = list(message_ids)
        self.opened = 0
        self.deleted: list[str] = []
        self.witness: list[str] = []

    @contextmanager
    def factory(self) -> Iterator[Any]:
        """Untyped on the way out: this stands in for a ``runic.ogm.Session``."""
        self.opened += 1
        yield self

    def all_rows(
        self, statement: Any, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        cypher = statement.build()[0]
        values = dict(params or {})
        if "DETACH DELETE" in cypher:
            self.deleted.extend(values["ids"])
            return [{"removed": len(values["ids"])}]
        if "DELETE " in cypher:
            return [{"removed": 0}]
        if "<>" in cypher:
            return []
        self.witness.append("page")
        if values["after"]:
            return []
        return [{"id": one} for one in self.message_ids]


class ExplodingGraph(RecordingGraph):
    """A graph that fails the way an unreachable store would."""

    @contextmanager
    def factory(self) -> Iterator[Any]:
        self.opened += 1
        raise ConnectionError("the graph is not answering")
        yield self  # pragma: no cover - unreachable, keeps this a generator


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail-archive.db'}")
    async with created.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> SessionFactory:
    """One session per call, committed on the way out."""
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def open_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return open_session


@pytest.fixture
async def account_id(sessions: SessionFactory) -> int:
    """A mailbox with a history: two archived messages, a cursor, a failure."""
    async with sessions() as session:
        account = MailAccountEntity(
            provider="fake",
            display_name="Work",
            email_address="jens@example.com",
            last_sync_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
            last_error="a token expired once",
        )
        session.add(account)
        await session.flush()
        await ArchivedMessageRepository().record_many(
            session, account.id, {"g-1": "m1@example.com", "g-2": "m2@example.com"}
        )
        await SyncCheckpointRepository().upsert_cursor(
            session, account.id, "full", "page-token", 2
        )
        await FailedMessageRepository().record(session, account.id, "g-3", "permanent")
        return account.id


@pytest.fixture
def graph() -> RecordingGraph:
    return RecordingGraph(["m1@example.com", "m2@example.com"])


@pytest.fixture
def eraser(graph: RecordingGraph, sessions: SessionFactory) -> AccountEraser:
    return AccountEraser(graph_session=graph.factory, database_session=sessions)


async def _account(sessions: SessionFactory, account_id: int) -> MailAccountEntity:
    async with sessions() as session:
        found = await session.get(MailAccountEntity, account_id)
        assert found is not None
        return found


class TestClearingAMailbox:
    async def test_reports_what_came_out_of_each_store(
        self, eraser: AccountEraser, account_id: int
    ) -> None:
        counts = await eraser.erase(account_id)

        assert counts == EraseCounts(
            messages=2, copies=0, archived_rows=2, checkpoints=1, failures=1
        )

    async def test_empties_both_stores(
        self,
        eraser: AccountEraser,
        graph: RecordingGraph,
        sessions: SessionFactory,
        account_id: int,
    ) -> None:
        """Either half alone leaves a mailbox that cannot be imported again."""
        await eraser.erase(account_id)

        assert graph.deleted == ["m1@example.com", "m2@example.com"]
        async with sessions() as session:
            assert await ArchivedMessageRepository().count(session) == 0
            assert await SyncCheckpointRepository().count(session) == 0
            assert await FailedMessageRepository().count(session) == 0

    async def test_the_mailbox_itself_survives(
        self, eraser: AccountEraser, sessions: SessionFactory, account_id: int
    ) -> None:
        """Clearing is not deleting — the whole point is to import it again."""
        await eraser.erase(account_id)

        account = await _account(sessions, account_id)
        assert account.email_address == "jens@example.com"
        assert account.enabled is True

    async def test_the_mailbox_stops_claiming_a_last_sync(
        self, eraser: AccountEraser, sessions: SessionFactory, account_id: int
    ) -> None:
        """A date on a page that now shows nothing is a date that misleads."""
        account = await _account(sessions, account_id)
        assert account.last_sync_at is not None

        await eraser.erase(account_id)

        cleared = await _account(sessions, account_id)
        assert cleared.last_sync_at is None
        assert cleared.last_error is None

    async def test_leaves_another_mailbox_untouched(
        self, eraser: AccountEraser, sessions: SessionFactory, account_id: int
    ) -> None:
        async with sessions() as session:
            other = MailAccountEntity(
                provider="fake",
                display_name="Private",
                email_address="other@example.com",
            )
            session.add(other)
            await session.flush()
            await ArchivedMessageRepository().record_many(
                session, other.id, {"g-9": "m9@example.com"}
            )
            other_id = other.id

        await eraser.erase(account_id)

        async with sessions() as session:
            known = await ArchivedMessageRepository().find_known_provider_ids(
                session, other_id, ["g-9"]
            )
        assert known == {"g-9"}

    async def test_clearing_an_empty_mailbox_is_a_confident_nought(
        self, sessions: SessionFactory, account_id: int
    ) -> None:
        eraser = AccountEraser(
            graph_session=RecordingGraph([]).factory, database_session=sessions
        )
        await eraser.erase(account_id)

        counts = await eraser.erase(account_id)

        assert counts == EraseCounts()


class TestWhatItRefuses:
    async def test_a_running_import_stops_the_clear_out(
        self, eraser: AccountEraser, sessions: SessionFactory, account_id: int
    ) -> None:
        """The job writes to exactly what this would delete."""
        async with sessions() as session:
            session.add(
                MailSyncJobEntity(
                    kind=SyncJobKind.IMPORT,
                    account_id=account_id,
                    state=SyncJobState.RUNNING,
                )
            )

        with pytest.raises(AccountBusy, match="still"):
            await eraser.erase(account_id)

    async def test_a_queued_import_stops_it_too(
        self, eraser: AccountEraser, sessions: SessionFactory, account_id: int
    ) -> None:
        """It starts the moment a worker frees up — mid clear-out, if allowed."""
        async with sessions() as session:
            session.add(
                MailSyncJobEntity(kind=SyncJobKind.IMPORT, account_id=account_id)
            )

        with pytest.raises(AccountBusy):
            await eraser.erase(account_id)

    async def test_a_refusal_deletes_nothing_at_all(
        self,
        eraser: AccountEraser,
        graph: RecordingGraph,
        sessions: SessionFactory,
        account_id: int,
    ) -> None:
        """The check runs before either store is opened, not between them."""
        async with sessions() as session:
            session.add(
                MailSyncJobEntity(
                    kind=SyncJobKind.IMPORT,
                    account_id=account_id,
                    state=SyncJobState.RUNNING,
                )
            )

        with pytest.raises(AccountBusy):
            await eraser.erase(account_id)

        assert graph.opened == 0
        async with sessions() as session:
            assert await ArchivedMessageRepository().count(session) == 2

    async def test_a_finished_import_is_no_obstacle(
        self, eraser: AccountEraser, sessions: SessionFactory, account_id: int
    ) -> None:
        async with sessions() as session:
            session.add(
                MailSyncJobEntity(
                    kind=SyncJobKind.IMPORT,
                    account_id=account_id,
                    state=SyncJobState.SUCCEEDED,
                )
            )

        counts = await eraser.erase(account_id)

        assert counts.messages == 2

    async def test_a_mailbox_that_is_gone_is_said_to_be_gone(
        self, eraser: AccountEraser
    ) -> None:
        """Not a confident nought: a clear-out addressed at nothing is a mistake."""
        with pytest.raises(LookupError, match="404"):
            await eraser.erase(404)


class TestTheOrderOfTheTwoStores:
    async def test_the_graph_goes_first_and_the_ledgers_survive_its_failure(
        self, sessions: SessionFactory, account_id: int
    ) -> None:
        """The recoverable failure mode, and the reason for the order.

        Interrupted here, the mailbox still has its ledgers — so the graph
        pass, which is re-runnable by construction, can simply be run again.
        The reverse order would leave messages in the archive that the account
        could never re-import, because their provider ids come back unseen and
        the writer finds the canonical id already there.
        """
        eraser = AccountEraser(
            graph_session=ExplodingGraph([]).factory, database_session=sessions
        )

        with pytest.raises(ConnectionError):
            await eraser.erase(account_id)

        async with sessions() as session:
            assert await ArchivedMessageRepository().count(session) == 2
            assert await SyncCheckpointRepository().count(session) == 1

    async def test_the_three_ledgers_are_cleared_in_one_transaction(
        self, graph: RecordingGraph, sessions: SessionFactory, account_id: int
    ) -> None:
        """One decision, so one session — and therefore one commit.

        Two of the three are what the import reads to decide what it has
        already done, so a clear-out that committed one and then failed would
        leave the mailbox importable in a way that skips exactly the wrong
        messages.
        """
        opened: list[int] = []

        @asynccontextmanager
        async def counted() -> AsyncIterator[AsyncSession]:
            opened.append(1)
            async with sessions() as session:
                yield session

        eraser = AccountEraser(graph_session=graph.factory, database_session=counted)

        await eraser.erase(account_id)

        # One to check the mailbox is idle, one for all three ledgers.
        assert len(opened) == 2
        assert graph.opened == 1
