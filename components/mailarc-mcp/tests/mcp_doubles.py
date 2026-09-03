"""The archive the tool tests read through, and nothing that asserts anything.

Two test modules need the same five stubs — ``test_mcp_server.py`` for the six
tools §7.5 asked for, ``test_mcp_analysis_tools.py`` for the four the analysis
phase added — and a second copy of them would be a second contract: a stub that
drifted would let one module pass while the other tested something the real
reader could not do. So they sit here, the way ``planted_graph.py`` sits beside
the analytics tests and ``worker_doubles.py`` beside the worker's.

A plain module and not ``conftest.py`` on purpose. This component is an
optional extra, and the conftest next door exists to leave the whole directory
uncollected on a checkout that resolved without it — a conftest importing
``mailarc_mcp`` at module scope would raise the very ``ModuleNotFoundError``
that file prevents. Nothing imports this one but the test modules the conftest
already skips.

The stubs are **subclasses of the real readers** rather than duck-typed
doubles. Two reasons: the type checker sees the same class the application
passes, and a stub cannot drift into answering something the real reader could
not. Their session factory raises, which turns "a tool opened a graph
connection" from an invisible slowdown into a failed test — this server must be
usable against an archive that is not running, and that is exactly the property
a stub with a working session would hide.
"""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.tools.base import TextContent
from runic.ogm import Session

from mailarc_analytics import AnalyticsReader, TemplateDirection
from mailarc_analytics.queries.model import (
    ArchiveTotals,
    CoAddressedRow,
    ImportantMessageRow,
    TemplateRow,
    TopicRow,
)
from mailarc_analytics.queries.reports import REPORT_LIMIT
from mailarc_analytics.semantic import (
    SearchHit,
    SearchKind,
    SearchRequest,
    SearchResult,
    SemanticConfig,
    SemanticSearch,
)
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import MessageSummary, TagSummary
from mailarc_core.archive.reader import ArchiveReader
from mailarc_core.archive.tags import MEMBER_PAGE, TagStore
from mailarc_mcp.server.model import Conversation, TopicMessages
from mailarc_mcp.server.reads import ArchiveAccess
from mailarc_mcp.server.server import build_server

MARCH = datetime(2026, 3, 12, 9, 0, tzinfo=UTC)
AUGUST = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)

VERSION = "1.0.0"
"""What a client would see in ``serverInfo``.

A literal, because the version is the *application's* and this component is not
allowed to know which application installed it — ``app/mcp_server.py`` reads it
off the distribution and passes it in.
"""


def never_session() -> AbstractContextManager[Session]:
    """The session factory of a reader that must not reach the graph."""
    raise AssertionError("a tool opened a graph session it should not have")


def unbuilt() -> Never:
    """The reader factory of an access object that answers from stubs instead.

    :class:`StubAccess` overrides every accessor, so nothing should ever reach
    the factories underneath them. Raising rather than returning a dummy means
    a method that stopped being overridden fails here instead of quietly
    answering from something nobody wrote.
    """
    raise AssertionError("a tool asked for a reader the stub was answering for")


class StubAnalytics(AnalyticsReader):
    """The derived reader with canned rows, remembering what it was asked."""

    def __init__(
        self,
        *,
        pairs: Sequence[CoAddressedRow] = (),
        clusters: Sequence[TopicRow] = (),
        forms: Sequence[TemplateRow] = (),
        important: Sequence[ImportantMessageRow] = (),
        totals: ArchiveTotals | None = None,
    ) -> None:
        super().__init__(graph_session=never_session)
        self._pairs = tuple(pairs)
        self._clusters = tuple(clusters)
        self._forms = tuple(forms)
        self._important = tuple(important)
        self._totals = totals or ArchiveTotals()
        self.asked: list[tuple[str, int]] = []

    def top_co_addressed(
        self, *, limit: int = REPORT_LIMIT
    ) -> tuple[CoAddressedRow, ...]:
        self.asked.append(("co_addressed", limit))
        return self._pairs

    def topics(self, *, limit: int = REPORT_LIMIT) -> tuple[TopicRow, ...]:
        self.asked.append(("topics", limit))
        return self._clusters

    def templates(
        self, direction: TemplateDirection, *, limit: int = REPORT_LIMIT
    ) -> tuple[TemplateRow, ...]:
        self.asked.append((f"templates:{direction.value}", limit))
        return self._forms

    def important_messages(
        self, *, limit: int = REPORT_LIMIT
    ) -> tuple[ImportantMessageRow, ...]:
        self.asked.append(("important", limit))
        return self._important

    def totals(self) -> ArchiveTotals:
        return self._totals


class StubArchive(ArchiveReader):
    """The ground-truth reader with a canned listing."""

    def __init__(
        self, summaries: Sequence[MessageSummary] = (), *, store: Path | None = None
    ) -> None:
        super().__init__(
            graph_session=never_session,
            blobs=BlobStore(ArchiveConfig(store_dir=store or Path("/dev/null/never"))),
        )
        self._canned = list(summaries)
        self.asked: list[tuple[int, int]] = []
        self.hydrated: list[list[str]] = []

    def list_messages(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[MessageSummary]:
        self.asked.append((limit, offset))
        return self._canned[offset : offset + limit]

    def messages_by_ids(self, ids: list[str]) -> list[MessageSummary]:
        """The canned summaries these ids name, in the caller's order.

        The real reader leaves out an id the graph no longer holds rather than
        answering with a hole, and so does this one: a tool that assumed a
        summary per id would raise on an archive somebody deleted from between
        two reads.
        """
        self.hydrated.append(list(ids))
        known = {one.id: one for one in self._canned}
        return [known[one] for one in ids if one in known]


class StubTags(TagStore):
    """The annotation layer with canned tags and canned membership."""

    def __init__(
        self,
        tags: Sequence[TagSummary] = (),
        members: dict[str, Sequence[str]] | None = None,
    ) -> None:
        super().__init__(graph_session=never_session)
        self._tags = tuple(tags)
        self._members = {key: tuple(value) for key, value in (members or {}).items()}
        self.asked: list[tuple[str, int, int]] = []
        self.listed = 0

    def list_tags(self) -> tuple[TagSummary, ...]:
        self.listed += 1
        return self._tags

    def members(
        self, tag_id: str, *, limit: int = MEMBER_PAGE, offset: int = 0
    ) -> tuple[str, ...]:
        self.asked.append((tag_id, limit, offset))
        return self._members.get(tag_id, ())[offset : offset + limit]


class StubSearch(SemanticSearch):
    """Both search paths, answering from a canned result — or refusing."""

    def __init__(self, result: SearchResult, *, error: Exception | None = None) -> None:
        super().__init__(
            graph_session=never_session, config=SemanticConfig(), embedder=None
        )
        self._result = result
        self._error = error
        self.requests: list[SearchRequest] = []

    async def search(self, request: SearchRequest) -> SearchResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._result


class StubAccess(ArchiveAccess):
    """One archive the tools read through, assembled per test.

    Subclassed rather than injected field by field, because
    :class:`ArchiveAccess` is what the application passes and a test that stood
    beside it would stop proving anything the day the real one gained a method.
    The factories the base class insists on are still supplied and still raise:
    every accessor below is overridden, so reaching one would mean a method had
    silently stopped being a stub.
    """

    def __init__(
        self,
        *,
        analytics: AnalyticsReader | None = None,
        archive: ArchiveReader | None = None,
        search: SemanticSearch | None = None,
        tags: TagStore | None = None,
        conversation: Conversation | None = None,
        topic: TopicMessages | None = None,
    ) -> None:
        super().__init__(
            graph_session=never_session,
            analytics=unbuilt,
            archive=unbuilt,
            search=unbuilt,
            tags=unbuilt,
        )
        self._stub_analytics = analytics or StubAnalytics()
        self._stub_archive = archive or StubArchive()
        self._stub_search = search or StubSearch(SearchResult(kind=SearchKind.FULLTEXT))
        self._stub_tags = tags or StubTags()
        self._stub_conversation = conversation
        self._stub_topic = topic
        self.asked_for: list[tuple[str, int]] = []
        self.asked_topic: list[tuple[str, int]] = []

    def analytics(self) -> AnalyticsReader:
        return self._stub_analytics

    def archive(self) -> ArchiveReader:
        return self._stub_archive

    def search(self) -> SemanticSearch:
        return self._stub_search

    def tags(self) -> TagStore:
        return self._stub_tags

    def conversation(self, message_id: str, limit: int) -> Conversation | None:
        self.asked_for.append((message_id, limit))
        return self._stub_conversation

    def topic(self, topic_id: str, limit: int) -> TopicMessages | None:
        self.asked_topic.append((topic_id, limit))
        return self._stub_topic


def hit(message_id: str = "m1", score: float = 0.5) -> SearchHit:
    return SearchHit(
        message_id=message_id,
        score=score,
        subject="Angebot 4711",
        sent_at=MARCH,
        sender="anna@kunde.example",
    )


def failure_text(answer: CallToolResult) -> str:
    """The sentence a caller reads off a failed call.

    A result's content is a union of five block types and only one of them
    carries text; narrowing it here keeps every assertion above about the
    message rather than about the shape of the protocol.
    """
    block = answer.content[0]
    assert isinstance(block, TextContent)
    return block.text


def client_over(access: ArchiveAccess) -> Client:
    """A client speaking to a server that reads through *access*."""
    return Client(build_server(access, version=VERSION))
