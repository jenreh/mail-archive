"""What a model on the other end of the socket actually gets.

Every tool is exercised through a real ``Client``, in memory: no subprocess, no
socket, no JSON-RPC over a pipe. Spawning the entry point and speaking the
protocol by hand would test the transport, which FastMCP already tests. What is
at stake here is the *contract* — a tool's name, the schema a model reads
before it calls anything, and the sentence it gets back when the archive cannot
answer — and that is what these tests assert.

The readers are stubs, and they are **subclasses of the real ones** rather than
duck-typed doubles. Two reasons: the type checker sees the same class the
application passes, and the stub cannot drift into answering something the real
reader could not. Their session factory raises, which turns "a tool opened a
graph connection" from an invisible slowdown into a failed test — this server
must be usable against an archive that is not running, and that is exactly the
property a stub with a working session would hide.

Nothing here imports ``app``, and that is the point of the component: the four
factories an :class:`ArchiveAccess` holds are supplied by whoever builds it, so
a test supplies its own and the composition root supplies the real ones. What
the *application* wires into them is ``tests/test_mcp_server.py``'s business.

The graph half of the tools — the statements, the projections, a real
conversation — is proved in ``test_mcp_server_local.py`` against a vendored
FalkorDB. Here the graph is deliberately absent.
"""

import ast
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult
from fastmcp.tools.base import TextContent
from runic.ogm import Session

from mailarc_analytics import AnalyticsReader, TemplateDirection
from mailarc_analytics.queries.model import (
    ArchiveTotals,
    CoAddressedRow,
    TemplateRow,
    TopicRow,
)
from mailarc_analytics.queries.reports import REPORT_LIMIT
from mailarc_analytics.semantic import (
    MAX_HITS,
    NO_EMBEDDER,
    SearchHit,
    SearchKind,
    SearchRequest,
    SearchResult,
    SemanticConfig,
    SemanticSearch,
)
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import MessageLabel, MessageSummary
from mailarc_core.archive.reader import ArchiveReader
from mailarc_core.mail.errors import MailPermanentError, MailTransientError
from mailarc_mcp.server import server as mcp_server
from mailarc_mcp.server.model import Conversation, ConversationMessage
from mailarc_mcp.server.reads import ArchiveAccess
from mailarc_mcp.server.server import (
    MAX_ROWS,
    MAX_THREAD,
    NO_MESSAGES,
    NOT_DERIVED,
    UNREACHABLE,
    build_server,
)

MARCH = datetime(2026, 3, 12, 9, 0, tzinfo=UTC)
AUGUST = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)

VERSION = "1.0.0"
"""What a client would see in ``serverInfo``.

A literal, because the version is the *application's* and this component is not
allowed to know which application installed it — ``app/mcp_server.py`` reads it
off the distribution and passes it in.
"""

TOOLS = {
    "search_messages",
    "co_recipients",
    "topics",
    "templates",
    "thread",
    "timeline",
}
"""The six §7.5 names. Written out rather than read off the server, so adding
or renaming a tool is a decision somebody makes in a diff."""

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "src" / "mailarc_mcp"

COMPONENT = (
    SOURCE / "__init__.py",
    SOURCE / "server" / "__init__.py",
    SOURCE / "server" / "server.py",
    SOURCE / "server" / "reads.py",
    SOURCE / "server" / "model.py",
    HERE / "test_mcp_server.py",
    HERE / "test_mcp_server_local.py",
)
"""Every file in this component, resolved from this one rather than from the
working directory — the component's tests run from the repository root in a
whole-workspace run and from here in a component-only one."""


def _never() -> AbstractContextManager[Session]:
    """The session factory of a reader that must not reach the graph."""
    raise AssertionError("a tool opened a graph session it should not have")


def _unbuilt() -> Never:
    """The reader factory of an access object that answers from stubs instead.

    :class:`StubAccess` overrides all four accessors, so nothing should ever
    reach the factories underneath them. Raising rather than returning a dummy
    means a method that stopped being overridden fails here instead of quietly
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
        totals: ArchiveTotals | None = None,
    ) -> None:
        super().__init__(graph_session=_never)
        self._pairs = tuple(pairs)
        self._clusters = tuple(clusters)
        self._forms = tuple(forms)
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

    def totals(self) -> ArchiveTotals:
        return self._totals


class StubArchive(ArchiveReader):
    """The ground-truth reader with a canned listing."""

    def __init__(
        self, summaries: Sequence[MessageSummary] = (), *, store: Path | None = None
    ) -> None:
        super().__init__(
            graph_session=_never,
            blobs=BlobStore(ArchiveConfig(store_dir=store or Path("/dev/null/never"))),
        )
        self._canned = list(summaries)
        self.asked: list[tuple[int, int]] = []

    def list_messages(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[MessageSummary]:
        self.asked.append((limit, offset))
        return self._canned[offset : offset + limit]


class StubSearch(SemanticSearch):
    """Both search paths, answering from a canned result — or refusing."""

    def __init__(self, result: SearchResult, *, error: Exception | None = None) -> None:
        super().__init__(graph_session=_never, config=SemanticConfig(), embedder=None)
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
    The four factories the base class insists on are still supplied and still
    raise: every accessor below is overridden, so reaching one would mean a
    method had silently stopped being a stub.
    """

    def __init__(
        self,
        *,
        analytics: AnalyticsReader | None = None,
        archive: ArchiveReader | None = None,
        search: SemanticSearch | None = None,
        conversation: Conversation | None = None,
    ) -> None:
        super().__init__(
            graph_session=_never,
            analytics=_unbuilt,
            archive=_unbuilt,
            search=_unbuilt,
        )
        self._stub_analytics = analytics or StubAnalytics()
        self._stub_archive = archive or StubArchive()
        self._stub_search = search or StubSearch(SearchResult(kind=SearchKind.FULLTEXT))
        self._stub_conversation = conversation
        self.asked_for: list[tuple[str, int]] = []

    def analytics(self) -> AnalyticsReader:
        return self._stub_analytics

    def archive(self) -> ArchiveReader:
        return self._stub_archive

    def search(self) -> SemanticSearch:
        return self._stub_search

    def conversation(self, message_id: str, limit: int) -> Conversation | None:
        self.asked_for.append((message_id, limit))
        return self._stub_conversation


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
    carries text; narrowing it here keeps every assertion below about the
    message rather than about the shape of the protocol.
    """
    block = answer.content[0]
    assert isinstance(block, TextContent)
    return block.text


def client_over(access: ArchiveAccess) -> Client:
    """A client speaking to a server that reads through *access*."""
    return Client(build_server(access, version=VERSION))


@pytest.fixture
async def bare() -> AsyncIterator[Client]:
    """A client over the default stubs — enough for schema-level assertions."""
    async with client_over(StubAccess()) as connected:
        yield connected


async def test_the_server_names_the_six_tools_the_spec_asks_for(
    bare: Client,
) -> None:
    """§7.5 lists them; a model picks by name, so the names are the contract."""
    tools = {tool.name for tool in await bare.list_tools()}

    assert tools == TOOLS


async def test_every_tool_says_it_cannot_write(bare: Client) -> None:
    """A read-only hint lets a client call without asking its user first.

    Nothing here writes, and a server that failed to say so would be treated
    as though it might.
    """
    for tool in await bare.list_tools():
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


async def test_a_tools_description_is_its_docstring(bare: Client) -> None:
    """The docstring is the model-facing contract, so it has to reach the wire."""
    tools = {tool.name: tool for tool in await bare.list_tools()}

    assert tools["topics"].description is not None
    assert tools["topics"].description.startswith(
        "Groups of messages one analysis thinks belong to the same work."
    )
    assert "joined_by" in tools["topics"].description
    assert "suggestion" in tools["topics"].description


async def test_a_parameter_carries_its_own_explanation(bare: Client) -> None:
    """``Args:`` entries become per-parameter schema text — where a model reads
    them. A limit whose bound is undocumented gets guessed at."""
    schema = {tool.name: tool.inputSchema for tool in await bare.list_tools()}

    limit = schema["co_recipients"]["properties"]["limit"]
    assert limit["default"] == 20
    assert "1..100" in limit["description"]

    semantic = schema["search_messages"]["properties"]["semantic"]
    assert "embedder" in semantic["description"]

    direction = schema["templates"]["properties"]["direction"]
    assert direction["default"] == "sent"
    assert set(direction["enum"]) == {"sent", "received"}


async def test_the_answer_schema_explains_its_own_fields(bare: Client) -> None:
    """Attribute docstrings become the output schema's descriptions.

    The one field that is dangerous to read without its sentence is ``score``:
    it is comparable only within one search kind, and nothing but the schema
    says so.
    """
    tools = {tool.name: tool for tool in await bare.list_tools()}
    schema = tools["search_messages"].outputSchema

    assert schema is not None
    hits = schema["properties"]["hits"]["items"]
    assert "only" in hits["properties"]["score"]["description"]


async def test_listing_the_tools_needs_no_archive_at_all() -> None:
    """Discovery happens before anything is running, so it must not connect.

    The real :class:`ArchiveAccess` is used here on purpose — no stub — with
    four factories that raise if anybody calls them. So this fails if a reader
    is ever asked for at import or at server construction instead of inside a
    tool, which is the property that lets a client describe this server against
    a machine whose graph is down.
    """
    access = ArchiveAccess(
        graph_session=_never,
        analytics=_unbuilt,
        archive=_unbuilt,
        search=_unbuilt,
    )

    async with Client(build_server(access, version=VERSION)) as connected:
        assert {tool.name for tool in await connected.list_tools()} == TOOLS


async def test_full_text_search_answers_with_hits() -> None:
    """The path that works without an embedder, end to end through the wire."""
    search = StubSearch(SearchResult(kind=SearchKind.FULLTEXT, hits=(hit(),)))
    async with client_over(StubAccess(search=search)) as connected:
        answer = await connected.call_tool("search_messages", {"query": "Angebot"})

    assert answer.is_error is False
    assert answer.data.kind == "fulltext"
    assert [one.message_id for one in answer.data.hits] == ["m1"]
    assert answer.data.hits[0].sent_at == MARCH
    assert search.requests[0].text == "Angebot"
    assert search.requests[0].kind is SearchKind.FULLTEXT


async def test_a_half_embedded_archive_says_so_beside_its_hits() -> None:
    """The coverage sentence is why the answer is an object and not a list.

    Ten hits over an archive that is a third embedded look exactly like ten
    hits over a small one; only this line tells them apart.
    """
    from mailarc_analytics.semantic import VectorCoverage

    search = StubSearch(
        SearchResult(
            kind=SearchKind.SEMANTIC,
            hits=(hit(),),
            coverage=VectorCoverage(model="nomic-embed-text", total=90, embedded=30),
        )
    )
    async with client_over(StubAccess(search=search)) as connected:
        answer = await connected.call_tool(
            "search_messages", {"query": "Angebot", "semantic": True}
        )

    assert answer.data.kind == "semantic"
    assert "60 of 90 messages have no nomic-embed-text embedding" in answer.data.notice


async def test_semantic_search_without_an_embedder_is_an_error_not_a_blank() -> None:
    """The phase's definition of done, asserted where a model would hit it.

    A real :class:`SemanticSearch` with no embedder, so the sentence under test
    is the component's own — a copy here would pass while the message a user
    reads had drifted. Its session factory raises, which also proves the
    refusal happens before a graph is opened.
    """
    search = SemanticSearch(graph_session=_never, config=SemanticConfig())
    async with client_over(StubAccess(search=search)) as connected:
        answer = await connected.call_tool(
            "search_messages",
            {"query": "Angebot", "semantic": True},
            raise_on_error=False,
        )

    assert answer.is_error is True
    assert answer.data is None
    message = failure_text(answer)
    assert message.strip().endswith(NO_EMBEDDER), (
        "a model should read the component's own sentence, not a paraphrase"
    )


async def test_a_search_limit_is_clamped_rather_than_refused() -> None:
    """A model cannot know this archive's size and will ask for ten thousand.

    Answering "invalid limit" spends a round trip teaching it a bound it could
    not have known; clamping answers the question it meant to ask.
    """
    search = StubSearch(SearchResult(kind=SearchKind.FULLTEXT))
    async with client_over(StubAccess(search=search)) as connected:
        await connected.call_tool(
            "search_messages", {"query": "Angebot", "limit": 10_000}
        )
        await connected.call_tool("search_messages", {"query": "Angebot", "limit": 0})

    assert [one.limit for one in search.requests] == [MAX_HITS, 1]


async def test_co_recipients_reads_the_derived_pairs() -> None:
    """A1 as a model sees it: two addresses, a count, and a span."""
    analytics = StubAnalytics(
        pairs=(
            CoAddressedRow(
                left_id="anna@kunde.example",
                right_id="jens@nordlicht.example",
                together=7,
                first_seen=MARCH,
                last_seen=AUGUST,
            ),
        )
    )
    async with client_over(StubAccess(analytics=analytics)) as connected:
        answer = await connected.call_tool("co_recipients", {"limit": 5})

    assert [one.address_a for one in answer.data] == ["anna@kunde.example"]
    assert answer.data[0].messages_together == 7
    assert answer.data[0].last_seen == AUGUST
    assert analytics.asked == [("co_addressed", 5)]


async def test_a_listing_limit_is_clamped_at_both_ends() -> None:
    """Zero would render as an empty archive: every statement ends in
    ``LIMIT $limit`` and ``LIMIT 0`` is legal Cypher returning nothing."""
    analytics = StubAnalytics(
        pairs=(CoAddressedRow(left_id="a@x.example", right_id="b@x.example"),)
    )
    async with client_over(StubAccess(analytics=analytics)) as connected:
        await connected.call_tool("co_recipients", {"limit": 5_000})
        await connected.call_tool("co_recipients", {"limit": 0})

    assert analytics.asked == [("co_addressed", MAX_ROWS), ("co_addressed", 1)]


async def test_a_conversation_limit_is_clamped_at_both_ends() -> None:
    """The other clamp the module docstring promises, and the one no test held.

    A thread is allowed to be longer than a listing — 200 against 100 — so it
    has its own helper, and a refactor that dropped it would send
    ``LIMIT 10000001`` into the graph and a thousand-message mailing-list
    thread with full previews into a model's context.
    """
    access = StubAccess(conversation=Conversation(thread_id="t1", subject="s"))
    async with client_over(access) as connected:
        await connected.call_tool("thread", {"message_id": "m1", "limit": 10_000_000})
        await connected.call_tool("thread", {"message_id": "m1", "limit": 0})

    assert access.asked_for == [("m1", MAX_THREAD), ("m1", 1)]


async def test_a_message_id_reaches_the_read_exactly_as_it_arrived() -> None:
    """``thread`` is the one tool whose read is a query-builder statement
    rather than a catalogue constant, so "the value is bound, never
    concatenated" is worth an assertion rather than an argument. Stripping a
    quote would be as wrong as passing one through unbound: the id is a key,
    and an id that is silently altered looks up the wrong message."""
    hostile = "m1' OR 1=1 --\u0000 \\ {$ne: null}"
    access = StubAccess(conversation=None)
    async with client_over(access) as connected:
        answer = await connected.call_tool(
            "thread", {"message_id": hostile}, raise_on_error=False
        )

    assert access.asked_for == [(hostile, 50)]
    assert answer.is_error is True, "no such message, which is the honest answer"


async def test_nothing_derived_yet_is_explained_rather_than_returned_empty() -> None:
    """An empty listing is ambiguous, and the ambiguity costs a reader dearly.

    "No topics" reads as "this archive holds no projects"; the truth is that a
    job has not run. The extra six counts are paid only on this path.
    """
    analytics = StubAnalytics(totals=ArchiveTotals(messages=41))
    async with client_over(StubAccess(analytics=analytics)) as connected:
        answer = await connected.call_tool("topics", {}, raise_on_error=False)

    assert answer.is_error is True
    assert failure_text(answer) == NOT_DERIVED
    assert "rebuild-derived" in failure_text(answer)


async def test_a_derived_archive_with_nothing_of_this_sort_answers_empty() -> None:
    """The third reading of an empty listing, and the only honest empty one.

    Templates exist, just none in the direction asked for. Raising here would
    send a reader to run a job that has already run.
    """
    analytics = StubAnalytics(totals=ArchiveTotals(messages=41, templates=6))
    async with client_over(StubAccess(analytics=analytics)) as connected:
        answer = await connected.call_tool(
            "templates", {"direction": "received"}, raise_on_error=False
        )

    assert answer.is_error is False
    assert answer.data == []


async def test_an_empty_archive_is_named_as_such() -> None:
    """The other half of the same ambiguity: nothing has been imported yet."""
    async with client_over(StubAccess(analytics=StubAnalytics())) as connected:
        answer = await connected.call_tool("co_recipients", {}, raise_on_error=False)

    assert answer.is_error is True
    assert failure_text(answer) == NO_MESSAGES


async def test_an_embedding_topic_is_marked_as_a_suggestion() -> None:
    """§6.2's whole point: a ``ref`` cluster is a fact, an ``embedding`` one is
    evidence. A model that cannot tell them apart will report a guess."""
    analytics = StubAnalytics(
        clusters=(
            TopicRow(id="t1", label="Angebot 4711", method="ref", messages=5),
            TopicRow(id="t2", label="Umzug", method="embedding", messages=3),
        )
    )
    async with client_over(StubAccess(analytics=analytics)) as connected:
        answer = await connected.call_tool("topics", {})

    assert [one.joined_by for one in answer.data] == ["ref", "embedding"]
    assert [one.is_suggestion for one in answer.data] == [False, True]


async def test_templates_are_asked_for_one_direction_at_a_time() -> None:
    """Only what you write yourself is automatable, so the split is the answer.

    The direction arrives as its enum value over the wire; that it reaches the
    reader as :class:`TemplateDirection` is what keeps the statement's bound
    parameter a string the graph understands.
    """
    analytics = StubAnalytics(
        forms=(
            TemplateRow(
                id="tpl1",
                direction=TemplateDirection.RECEIVED,
                occurrences=9,
                automation_score=0.42,
                sample_text="Ihre Rechnung",
                first_seen=MARCH,
                last_seen=AUGUST,
            ),
        )
    )
    async with client_over(StubAccess(analytics=analytics)) as connected:
        answer = await connected.call_tool("templates", {"direction": "received"})

    assert analytics.asked == [("templates:received", 20)]
    assert answer.data[0].direction == "received"
    assert answer.data[0].occurrences == 9


async def test_reading_a_conversation() -> None:
    """The thread tool's happy path: the exchange, oldest first."""
    conversation = Conversation(
        thread_id="1:T1",
        subject="Angebot 4711",
        messages=(
            ConversationMessage(
                message_id="m1", subject="Angebot 4711", sender="anna@kunde.example"
            ),
            ConversationMessage(
                message_id="m2",
                subject="Re: Angebot 4711",
                sender="jens@nordlicht.example",
            ),
        ),
        truncated=True,
    )
    access = StubAccess(conversation=conversation)
    async with client_over(access) as connected:
        answer = await connected.call_tool("thread", {"message_id": "m1"})

    assert [one.message_id for one in answer.data.messages] == ["m1", "m2"]
    assert answer.data.truncated is True
    assert access.asked_for == [("m1", 50)]


async def test_an_unknown_message_id_is_an_error_that_says_where_ids_come_from(
    bare: Client,
) -> None:
    """An empty conversation would read as "this message stands alone", which
    is a different and legitimate answer. So the unknown id raises instead."""
    answer = await bare.call_tool(
        "thread", {"message_id": "nope"}, raise_on_error=False
    )

    assert answer.is_error is True
    assert "'nope'" in failure_text(answer)
    assert "search_messages" in failure_text(answer)


async def test_the_timeline_flattens_what_a_row_prints() -> None:
    """Labels as names and a preview instead of a body — a model pays per token
    for anything it cannot act on."""
    archive = StubArchive(
        [
            MessageSummary(
                id="m9",
                sender_name="Anna Meier",
                sender_address="anna@kunde.example",
                subject="Angebot 4711",
                preview="Hallo Jens, anbei das Angebot.",
                sent_at=AUGUST,
                has_attachments=True,
                labels=(MessageLabel(name="Kunden"),),
            )
        ]
    )
    async with client_over(StubAccess(archive=archive)) as connected:
        answer = await connected.call_tool("timeline", {"limit": 5, "offset": -3})

    assert answer.data[0].labels == ["Kunden"]
    assert answer.data[0].sender_name == "Anna Meier"
    assert answer.data[0].has_attachments is True
    assert archive.asked == [(5, 0)], "a negative offset is read as the beginning"


async def test_a_graph_that_does_not_answer_never_speaks_for_itself() -> None:
    """A driver's own words can name a path inside this installation, and a
    tool result is the one thing a language model reads verbatim."""

    class Broken(StubAnalytics):
        def topics(self, *, limit: int = REPORT_LIMIT) -> tuple[TopicRow, ...]:
            raise RuntimeError("/Users/jens/.state/mailstore/de/ad/beef.eml is missing")

    async with client_over(StubAccess(analytics=Broken())) as connected:
        answer = await connected.call_tool("topics", {}, raise_on_error=False)

    assert answer.is_error is True
    assert failure_text(answer) == UNREACHABLE
    assert "mailstore" not in failure_text(answer)


async def test_an_exception_escaping_a_tool_is_masked_by_the_server() -> None:
    """The backstop under the translation, asserted on the real server object.

    Everything a tool can expect is a :class:`ToolError` already; this proves
    that a defect nobody anticipated still cannot put a private path on the
    wire.
    """
    server: FastMCP = build_server(StubAccess(), version=VERSION)

    @server.tool
    def leaky() -> str:
        """A defect, standing in for one nobody has written yet."""
        raise RuntimeError("/Users/jens/.state/mailstore/de/ad/beef.eml is missing")

    async with Client(server) as connected:
        answer = await connected.call_tool("leaky", {}, raise_on_error=False)

    assert answer.is_error is True
    assert failure_text(answer) == "Error calling tool 'leaky'"
    assert "mailstore" not in failure_text(answer)


async def test_an_unknown_argument_is_refused(bare: Client) -> None:
    """Strict validation, so a client's stray field fails loudly instead of
    being dropped — and the message does not echo what was sent."""
    answer = await bare.call_tool(
        "co_recipients", {"limit": 5, "sql": "DROP"}, raise_on_error=False
    )

    assert answer.is_error is True
    assert "DROP" not in failure_text(answer)


def test_fastmcp_logs_are_routed_into_the_host_applications_logging() -> None:
    """Masking protects the wire, not the log stream.

    FastMCP attaches its own stderr handlers at import and stops its records
    propagating, so an unmasked traceback would be written by a logger the host
    application does not configure — to the stream an MCP client captures.
    """
    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(logging.NullHandler())

    mcp_server.route_fastmcp_logging()

    assert fastmcp_logger.handlers == []
    assert fastmcp_logger.propagate is True


def test_the_server_imports_only_fastmcp_and_never_the_protocol_package() -> None:
    """``mcp`` is in the lock file only because ``fastmcp`` pulls it in.

    No ``pyproject.toml`` in this workspace declares it, so importing it
    directly is a dependency on an intermediary's resolution — and the day
    ``fastmcp`` vendors, renames or drops it, a *console script* fails at
    import and an MCP client shows a blank error. The classes are identical
    objects either way, which is what makes the fix free.
    """
    imported = {
        name
        for path in COMPONENT
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
        for name in (node.module,)
    } | {
        alias.name
        for path in COMPONENT
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    offenders = {name for name in imported if name == "mcp" or name.startswith("mcp.")}

    assert offenders == set(), (
        f"{sorted(offenders)} is an undeclared transitive dependency; "
        "fastmcp re-exports the same class objects"
    )


class TestWhatAnUpstreamRefusalSays:
    """``MailError`` wraps six tools, not just the embedding path.

    Two things went over the wire that should not have. The sentence named the
    embedding service for *any* ``MailError``, including ones an archive read
    can raise, and it interpolated the exception — whose text for the embedder
    path carries the provider's own error body and the resolved base URL, which
    on a corporate installation is an internal hostname.
    """

    async def test_the_endpoint_it_could_not_reach_is_not_named_to_the_caller(
        self,
    ) -> None:
        refused = MailTransientError(
            "the embedding endpoint returned 503 for "
            "http://models.internal.corp:11434/api/embed — overloaded"
        )
        access = StubAccess(
            search=StubSearch(SearchResult(kind=SearchKind.SEMANTIC), error=refused)
        )

        async with client_over(access) as connected:
            answer = await connected.call_tool(
                "search_messages",
                {"query": "rechnung", "semantic": True},
                raise_on_error=False,
            )

        assert answer.is_error is True
        assert "models.internal.corp" not in failure_text(answer)
        assert "503" not in failure_text(answer)

    async def test_it_does_not_assert_the_embedder_was_at_fault(self) -> None:
        """The clause catches every tool, and ``mailarc_core.mail.errors`` is a
        taxonomy the archive readers raise as well — so a full-text read that
        failed was flatly telling its caller that an embedding service it may
        never have configured could not be reached. Naming it as the *likely*
        cause is useful; asserting it is wrong, and the sentence has to offer
        the paths that need no embedder."""
        access = StubAccess(
            search=StubSearch(
                SearchResult(kind=SearchKind.FULLTEXT),
                error=MailPermanentError("the archive refused the read"),
            )
        )

        async with client_over(access) as connected:
            answer = await connected.call_tool(
                "search_messages", {"query": "rechnung"}, raise_on_error=False
            )

        said = failure_text(answer)
        assert answer.is_error is True
        assert said == mcp_server.UPSTREAM_REFUSED
        assert "the archive refused the read" not in said
        assert "likely" in said, "it may not have been the embedder at all"
        assert "Full-text search" in said, "the paths that need no embedder"

    async def test_the_detail_is_kept_where_an_operator_can_read_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fixed on the wire is not the same as thrown away: whoever has to
        fix the endpoint needs the status and the URL."""
        access = StubAccess(
            search=StubSearch(
                SearchResult(kind=SearchKind.SEMANTIC),
                error=MailTransientError("503 for http://models.internal.corp"),
            )
        )

        with caplog.at_level(logging.WARNING, logger="mailarc_mcp.server.server"):
            async with client_over(access) as connected:
                await connected.call_tool(
                    "search_messages",
                    {"query": "rechnung", "semantic": True},
                    raise_on_error=False,
                )

        assert "models.internal.corp" in caplog.text
