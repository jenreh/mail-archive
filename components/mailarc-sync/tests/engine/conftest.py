"""The fixtures both engine test modules run on.

Here rather than in either module because a full walk and a delta need exactly
the same world — three messages in a directory, a real SQLite file, one graph
session — and a fixture defined in one test module cannot be seen from the
other.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from appkit_commons.database.entities import Base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from engine_doubles import ADDRESS, FakeSession, build_engine, message_bytes
from mailarc_core.database.entities import MailAccountEntity
from mailarc_core.mail.model import MailProvider
from mailarc_sync.engine.engine import ImportEngine


@pytest.fixture
def mailbox(tmp_path: Path) -> Path:
    """Three messages in a directory, named so the listing order is known."""
    directory = tmp_path / "mailbox"
    directory.mkdir()
    for number in (1, 2, 3):
        (directory / f"m{number}.eml").write_bytes(message_bytes(number))
    return directory


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Any]:
    """A session factory over a real SQLite file, with appkit's commit rule.

    ``get_asyncdb_session`` commits when its block leaves cleanly and rolls
    back when it does not; the engine leans on exactly that, so the fixture
    copies it rather than handing out a bare session.
    """
    database_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mail.db'}")
    async with database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(database_engine, expire_on_commit=False)

    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as open_session:
            try:
                yield open_session
                await open_session.commit()
            except BaseException:
                await open_session.rollback()
                raise

    yield session
    await database_engine.dispose()


@pytest.fixture
async def account_id(database: Any) -> int:
    """The account row every relational table hangs off."""
    async with database() as session:
        account = MailAccountEntity(
            provider=MailProvider.FAKE,
            display_name="Fixtures",
            email_address=ADDRESS,
        )
        session.add(account)
        await session.flush()
        return account.id


@pytest.fixture
def graph() -> FakeSession:
    return FakeSession()


@pytest.fixture
def make_engine(
    tmp_path: Path, database: Any, graph: FakeSession
) -> Callable[..., ImportEngine]:
    """The real engine over the fixtures, with the knobs a test wants to turn."""

    def build(**overrides: Any) -> ImportEngine:
        return build_engine(
            tmp_path=tmp_path, database=database, graph=graph, **overrides
        )

    return build
