"""The archive as an MCP server: six read-only tools over the query catalogue.

§7.5's answer to "can I ask my archive questions" — and the answer is a *model
reading it*, never a model writing it. §3.2 draws that line: email already
carries an exact graph in its headers, so nothing a language model produces is
allowed to become ground truth. This server is where that rule is cashed in.
Everything here reads; nothing writes; every statement that reaches the store
is a named constant in ``mailarc_analytics.queries.catalog`` or a query-builder
statement compiled against the mapped models, and **no tool takes a query
string that reaches the graph**. A model can ask any question the six tools
express and no other.

It is a component and not a package under ``app/``, for the reason its README
gives: ``fastmcp`` is around sixty distributions and the desktop bundle has no
use for one of them, so the server has to be something an installation can
leave out. What that costs is the one thing a component may never do — build
itself from configuration (§4.1) — so :func:`build_server` is *handed* an
:class:`~mailarc_mcp.server.reads.ArchiveAccess` and a version string, and
``app/mcp_server.py`` is the thin process that binds both to the composition
root.

Three rules the code follows and a reviewer should hold it to:

**Every expected failure is a ``ToolError``.** No embedder configured, nothing
derived yet, an unknown message id, a graph that does not answer — each is a
state a person can fix, and each comes back as a sentence naming the fix.
``mask_error_details=True`` catches anything else, but masking is a backstop:
an ordinary exception escaping a tool body is a defect, because its traceback
goes to a log stream the client owns and this archive is private mail.

**No empty result stands in for a failure.** An empty list means the archive
holds nothing matching; every other reason for an empty answer raises. A user
who reads "no results" stops looking, which is exactly the damage §10's
definition of done is written to prevent.

**Every limit is clamped.** The caller is a model that cannot know this
archive's size and will ask for 10 000 rows. Clamping answers the question it
meant to ask instead of spending a round trip teaching it a bound.

Served over stdio by the ``mail-archive-mcp`` console script. Nothing in that
process may write to stdout: the JSON-RPC frames own it, and one stray line
makes the client drop the message it lands in.

**Only ``fastmcp`` is imported, never ``mcp``.** The protocol types live in
``mcp``, and ``fastmcp`` re-exports the ones a server needs —
``fastmcp.tools.base.ToolAnnotations`` *is* ``mcp.types.ToolAnnotations``, the
same class object. Reaching for ``mcp`` directly would be an import of a package
no ``pyproject.toml`` in this workspace declares: it is here only because
``fastmcp`` resolves to ``fastmcp-slim[client,server]``, which requires it, and
that is an intermediary's decision to change. This is a stdio server, so the
day it changed the failure would be a console script that dies at import and an
MCP client showing a blank error.

``…tools.base`` and not ``…tools.tool``: the latter resolves at runtime and only
at runtime. ``fastmcp/tools/__init__.py`` assigns it into ``sys.modules`` as a
backward-compatibility shim whose own comment says it is "safe to remove once
we're confident no external code imports from the old path" — so a type checker
cannot see it, and the package intends to delete it.
"""

import contextlib
import logging
from collections.abc import Iterator

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolAnnotations

from mailarc_analytics import TemplateDirection
from mailarc_analytics.derived.model import EMBEDDING_METHOD
from mailarc_analytics.queries.model import (
    CoAddressedRow,
    TemplateRow,
    TopicRow,
)
from mailarc_analytics.semantic import (
    SETTINGS_PAGE,
    STORED_WINS,
    SearchHit,
    SearchKind,
    SearchRequest,
    SearchResult,
    SemanticError,
)
from mailarc_core.archive.model import MessageSummary
from mailarc_core.mail.errors import MailError
from mailarc_mcp.server.model import (
    Conversation,
    CorrespondentPair,
    MessageHit,
    MessageTemplate,
    SearchAnswer,
    TimelineEntry,
    TopicCluster,
)
from mailarc_mcp.server.reads import ArchiveAccess

logger = logging.getLogger(__name__)

SERVER_NAME = "mail-archive"

INSTRUCTIONS = """\
Read-only access to one person's email archive: the messages they imported from
their own mailboxes, and what three analyses derived from them.

The archive is ground truth taken from the message headers — senders,
recipients, conversations, labels, attachments — so who wrote to whom is a
fact here, not an inference. On top of that sits a *derived* layer that a
rebuild job recomputes and throws away at will: groups of people who are
written to together, topics, and recurring templates. Anything in the derived
layer can be stale if the job has not run since the last import.

Start with search_messages or timeline to find a message, then thread to read
the exchange around it. co_recipients, topics and templates answer questions
about the archive as a whole rather than about one message.

Nothing here can change the archive, and nothing an assistant concludes is
written back to it.
"""
"""What the client shows a model before it picks a tool.

Worth its length: the one thing a model cannot guess is which answers are
facts read out of headers and which are an analysis that may be stale.
"""

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
"""The invariant of this whole server, stated to the client per tool.

Free to declare and worth declaring: a client that knows a tool cannot write
may call it without asking its user, and a mail archive that only ever answers
questions is exactly the case that permission prompt exists to distinguish.
"""

MAX_ROWS = 100
"""The most rows any listing returns, whatever was asked for.

Far below :data:`~mailarc_analytics.queries.reports.MAX_ROWS`'s five thousand,
because the reader is different: five thousand rows is a report a page
paginates, and for a model it is an archive dump paid for by the token. A
hundred is more than any question here needs and small enough that the wrong
question costs nothing.
"""

MAX_THREAD = 200
"""The most messages one conversation returns. Long mailing-list threads run
into the hundreds; past that a caller wants a search, not a transcript."""

NO_MESSAGES = (
    "The archive holds no messages yet. Import a mailbox first — nothing can "
    "be searched or derived until something has been archived."
)

NOT_DERIVED = (
    "Nothing has been derived from this archive yet. Correspondent pairs, "
    "topics and templates are computed by the derive job, not by the import: "
    "run it (`task graph:rebuild-derived`, or Rebuild on the /admin/insights "
    "page) and ask again. Searching messages and reading threads works without "
    "it."
)

UPSTREAM_REFUSED = (
    "An upstream service the archive depends on refused the request. If this "
    "was a semantic search, its embedding service is the likely one — check "
    f"that it is running and that the API key on the {SETTINGS_PAGE} page is "
    "right (or app_semantic_api_key, if this archive is configured from a "
    f"file, though {STORED_WINS}). Full-text search, correspondents, topics, "
    "templates and threads do not use it."
)
"""What a caller is told when a ``MailError`` reaches a tool.

Two things this sentence deliberately does not do. It does not *assert* that the
embedder is at fault: the ``except`` clause wraps all six tools and
``mailarc_core.mail.errors`` is a taxonomy the archive readers can raise too, so
naming the embedder outright told some callers to check a service they had never
configured. And it does not carry the exception's own text, which for the
embedder path quotes the provider's error body and the resolved base URL —
on a corporate installation an internal hostname, on the wire, in front of a
language model. The detail goes to the log, where the person who can fix the
endpoint is.
"""

UNREACHABLE = (
    "The mail archive did not answer: its graph database is not reachable "
    "right now. Start the mail archive application (which starts the database "
    "with it) and try again."
)
"""What a caller is told when a read fails for a reason it cannot name.

A fixed sentence and never the exception's own text. The driver's message can
carry a filesystem path out of this installation, and a tool result is the one
thing here that a language model reads verbatim.
"""


def build_server(access: ArchiveAccess, *, version: str) -> FastMCP:
    """The server, with its six tools bound to one way of reading the archive.

    Constructing it touches no configuration and opens no connection, so a
    client can list the tools against a machine whose archive is not running —
    which is what discovery does before it ever calls anything. Every reader is
    asked for inside a tool, on first use.

    Both arguments are required and neither has a default, because a default
    for either would be this component reading its installation. ``access``
    carries the four factories a tool reads through; ``version`` is what a
    client shows in ``serverInfo``, and it is the *application's* version
    rather than this package's — a bug report has to name the software a person
    installed. Left to itself FastMCP reports its own version, so a client would
    show the archive as 3.4.7 and the report would name the wrong project.
    """
    mcp = FastMCP(
        name=SERVER_NAME,
        version=version,
        instructions=INSTRUCTIONS,
        mask_error_details=True,
        strict_input_validation=True,
    )

    @mcp.tool(annotations=READ_ONLY)
    async def search_messages(
        query: str, limit: int = 20, semantic: bool = False
    ) -> SearchAnswer:
        """Find archived messages, by their words or by what they are about.

        Two different searches behind one tool. The default reads a full-text
        index over subject and body and finds the words asked for; it always
        works. With ``semantic=true`` the query is embedded and matched against
        message vectors, which finds mail *about* the subject even when it
        never uses the word — and needs an embedder to be configured and the
        embed job to have run. If it is not, this call fails with a sentence
        saying so; it never quietly answers with nothing.

        An empty ``hits`` list means the archive holds no matching message.
        Read ``notice`` if it is set: it says the answer is incomplete because
        part of the archive has no vectors yet.

        Args:
            query: plain words to look for. Search operators are removed before
                the archive sees them, so `-word`, `field:value` and `*` are
                read as the letters in them; ask twice rather than trying to
                express OR. Several words narrow the result, they do not widen
                it.
            limit: how many messages to return, best match first. Clamped to
                1..200; 20 is a screenful.
            semantic: search by meaning instead of by literal words. Requires a
                configured embedder — the call fails with an explanation if
                there is none, so full-text results are never silently
                substituted.
        """
        request = SearchRequest(
            text=query,
            kind=SearchKind.SEMANTIC if semantic else SearchKind.FULLTEXT,
            limit=limit,
        )
        with _translated("search_messages"):
            return _answer(await access.search().search(request))

    @mcp.tool(annotations=READ_ONLY)
    def co_recipients(limit: int = 20) -> list[CorrespondentPair]:
        """Which two addresses keep being written to in the same message.

        Answers "who works with whom", counted off the archive's own
        recipient lists: every message that names both addresses in To or Cc
        counts once. Blind copies are never counted — a Bcc recipient was
        written to without the others knowing, and pairing them would publish
        exactly what that header hides.

        Heaviest pair first. Read ``last_seen`` before drawing a conclusion: a
        pair with two hundred messages that stopped three years ago is a
        finished project, not a current working relationship.

        Empty is not a possible answer: if nothing has been derived yet, this
        call fails and says how to derive it.

        Args:
            limit: how many pairs to return, heaviest first. Clamped to 1..100.
        """
        with _translated("co_recipients"):
            rows = access.analytics().top_co_addressed(limit=_rows(limit))
            if not rows:
                _explain_empty(access, "co_addressed")
            return [_pair(row) for row in rows]

    @mcp.tool(annotations=READ_ONLY)
    def topics(limit: int = 20) -> list[TopicCluster]:
        """Groups of messages one analysis thinks belong to the same work.

        Built by joining messages that share something concrete — a ticket
        token, the provider's conversation, a normalised subject, an attachment
        by content hash, the same set of participants — and, where an embedder
        is configured, by semantic similarity for what none of those joined.

        **Read ``joined_by`` before believing a row.** Everything except
        ``embedding`` is a fact taken from the headers: the messages really do
        name the same ticket, or really are the same conversation. An
        ``embedding`` row is a suggestion — two texts landed close together in
        a vector space — and ``is_suggestion`` marks it. Present the two
        differently; the archive's own UI shows the method as a badge for this
        reason.

        A topic joined by two signals appears as two rows, one per signal.
        That is deliberate: summing them would hide the column that says how
        much the grouping is worth.

        Args:
            limit: how many rows to return, largest topic first. Clamped to
                1..100.
        """
        with _translated("topics"):
            rows = access.analytics().topics(limit=_rows(limit))
            if not rows:
                _explain_empty(access, "topics")
            return [_topic(row) for row in rows]

    @mcp.tool(annotations=READ_ONLY)
    def templates(
        direction: TemplateDirection = TemplateDirection.SENT, limit: int = 20
    ) -> list[MessageTemplate]:
        """Texts that get written again and again with barely a word changed.

        Lexical rather than semantic: these are messages with the same
        *wording*, found by fingerprinting the body — not messages about the
        same topic, which is what ``topics`` answers. The question behind it is
        "what could be automated", so the ranking weighs how often a text
        recurs, how regular the intervals are, and how short it is: a monthly
        status mail outranks two hundred identical newsletters.

        Sent and received are reported separately and never together, because
        only what the archive's owner writes is automatable. Received
        templates say something about the sender instead. An empty list here
        means the rebuild found no recurring text in *that direction* — it is
        an answer; a missing rebuild is an error instead.

        Args:
            direction: `sent` for texts the archive's owner writes — the
                automatable ones — or `received` for texts that keep arriving.
            limit: how many templates to return, best candidate first. Clamped
                to 1..100.
        """
        with _translated("templates"):
            rows = access.analytics().templates(direction, limit=_rows(limit))
            if not rows:
                _explain_empty(access, "templates")
            return [_template(row) for row in rows]

    @mcp.tool(annotations=READ_ONLY)
    def thread(message_id: str, limit: int = 50) -> Conversation:
        """Read the conversation one message belongs to, oldest first.

        The grouping is the provider's own: these messages were threaded by
        the mail service, not by a similarity guess. A message nobody replied
        to, or one from a provider that does not thread, comes back as a
        conversation of one with an empty ``thread_id`` — that is an answer,
        not a failure.

        Each message carries a preview rather than its full body: the quoted
        history and the signature are stripped, and a forty-message thread
        would otherwise be mostly the same text repeated. Check ``truncated``
        — when it is true, more messages exist than were returned.

        Args:
            message_id: the archive's own id for a message, as returned by
                search_messages or timeline. Not a provider id such as a Gmail
                message id; the call fails if the archive has no such message.
            limit: how many messages of the conversation to return, oldest
                first. Clamped to 1..200.
        """
        with _translated("thread"):
            found = access.conversation(message_id, _thread_rows(limit))
        if found is None:
            raise ToolError(
                f"The archive holds no message with id {message_id!r}. Ids come "
                "from search_messages and timeline and are the archive's own, "
                "not a provider's message id.",
                log_level=logging.DEBUG,
            )
        return found

    @mcp.tool(annotations=READ_ONLY)
    def timeline(limit: int = 20, offset: int = 0) -> list[TimelineEntry]:
        """Walk the archive by date, newest message first.

        What arrived and what was sent, in order, with the sender, the subject,
        the labels the provider filed it under and the opening of the body.
        The way to answer "what has been going on lately" or to page back
        through a period when there is no word to search for.

        An empty list means the archive holds nothing at or beyond this
        offset — with ``offset=0`` that means it holds no messages at all.

        Args:
            limit: how many messages to return. Clamped to 1..100.
            offset: how many of the newest messages to skip first, for paging
                further back. Negative values are read as 0.
        """
        with _translated("timeline"):
            rows = access.archive().list_messages(
                limit=_rows(limit), offset=max(0, offset)
            )
            return [_entry(row) for row in rows]

    return mcp


def route_fastmcp_logging() -> None:
    """Send FastMCP's own records through the host application's logging.

    ``import fastmcp`` attaches two rich handlers to stderr and sets
    ``propagate = False``, so by default its logs bypass everything the
    surrounding process configured. That matters because FastMCP logs the
    *unmasked* exception, traceback and source line for anything escaping a
    tool: masking protects the wire, not the log. Undoing the handlers and
    letting the records propagate means one configuration decides where they go
    and at what level.

    Silencing them outright was the alternative and is worse: a defect that
    leaves no trace anywhere is not a defect anybody fixes.

    Public and called from the entry point rather than from :func:`build_server`
    — it changes a process-wide logger, and a library that reconfigures logging
    as a side effect of being constructed is exactly what this is undoing.
    """
    fastmcp_logger = logging.getLogger("fastmcp")
    for handler in list(fastmcp_logger.handlers):
        fastmcp_logger.removeHandler(handler)
    fastmcp_logger.propagate = True


@contextlib.contextmanager
def _translated(tool: str) -> Iterator[None]:
    """Turn what the archive can raise into what a caller can act on.

    Three bands, and only the first one puts the exception's own words on the
    wire. The semantic package raises its own errors and every one of them is
    written for a person — "set app_semantic_provider", "run the embed job" —
    so those pass through verbatim. §7.6's taxonomy is an upstream refusal the
    caller can neither retry usefully nor fix; it gets
    :data:`UPSTREAM_REFUSED`, because its text quotes a provider's error body
    and a resolved base URL. Anything else is either an unreachable graph or a
    defect here, and gets :data:`UNREACHABLE` for the same reason one step
    further on: a driver's message may name a path inside this installation,
    and a tool result is read verbatim by a language model. Both fixed
    sentences are logged with their detail first, so nothing is lost to the
    person who can act on it.

    A context manager rather than a decorator so the ``await`` in
    :func:`search_messages` sits inside it without a second async-flavoured
    copy of the same three ``except`` clauses.
    """
    try:
        yield
    except SemanticError as error:
        raise ToolError(str(error), log_level=logging.DEBUG) from error
    except MailError as error:
        logger.warning("Tool %s was refused upstream: %s", tool, error)
        raise ToolError(UPSTREAM_REFUSED, log_level=logging.DEBUG) from error
    except ToolError:
        raise
    except Exception as error:
        logger.error(
            "Tool %s could not read the archive: %s", tool, type(error).__name__
        )
        logger.debug("What %s failed with", tool, exc_info=True)
        raise ToolError(UNREACHABLE) from error


def _explain_empty(archive: ArchiveAccess, kind: str) -> None:
    """Say why a derived listing came back empty — where there is a why.

    An empty listing means one of three things and only two of them are the
    reader's to fix: nothing has been imported, nothing has been derived, or
    the rebuild ran and found nothing of this sort. The first two raise with
    the remedy, because a model told "no topics" concludes the archive holds no
    projects and stops asking. The third returns the empty list, because it is
    the true answer — an archive with topics but no *received* template really
    has none.

    The six counts are paid only here, on the path where the answer would
    otherwise be ambiguous.
    """
    totals = archive.analytics().totals()
    if not totals.messages:
        raise ToolError(NO_MESSAGES, log_level=logging.DEBUG)
    derived = {
        "co_addressed": totals.co_addressed,
        "topics": totals.topics,
        "templates": totals.templates,
    }[kind]
    if not derived:
        raise ToolError(NOT_DERIVED, log_level=logging.DEBUG)
    logger.debug("Nothing matched in %d derived %s rows", derived, kind)


def _rows(limit: int) -> int:
    """A listing's row count, brought into range instead of refused.

    The caller cannot know this archive's size and a validation error would
    cost a round trip to teach it a bound; clamping answers the question it
    meant to ask. Zero has to be clamped as well as the big numbers: every
    listing behind these tools binds ``$limit`` into a trailing ``LIMIT``, and
    ``LIMIT 0`` is legal Cypher returning nothing, which would read as an empty
    archive.

    That the statements became query-builder objects changed nothing here —
    ``.limit(param("limit"))`` compiles to the same ``LIMIT $limit`` and the
    value still reaches the store as the caller's number. Measured: bound to
    zero, every listing comes back empty; bound to one, it comes back with a
    row.
    """
    return min(max(1, limit), MAX_ROWS)


def _thread_rows(limit: int) -> int:
    """The same clamp for a conversation, which is allowed to be longer."""
    return min(max(1, limit), MAX_THREAD)


def _answer(result: SearchResult) -> SearchAnswer:
    """A search result as the wire carries it, coverage warning included."""
    return SearchAnswer(
        kind=result.kind.value,
        hits=tuple(_hit(one) for one in result.hits),
        notice=result.notice,
    )


def _hit(hit: SearchHit) -> MessageHit:
    return MessageHit(
        message_id=hit.message_id,
        subject=hit.subject,
        sender=hit.sender,
        sent_at=hit.sent_at,
        score=hit.score,
    )


def _pair(row: CoAddressedRow) -> CorrespondentPair:
    return CorrespondentPair(
        address_a=row.left_id,
        address_b=row.right_id,
        messages_together=row.together,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
    )


def _topic(row: TopicRow) -> TopicCluster:
    return TopicCluster(
        topic_id=row.id,
        label=row.label,
        joined_by=row.method,
        is_suggestion=row.method == EMBEDDING_METHOD,
        messages=row.messages,
    )


def _template(row: TemplateRow) -> MessageTemplate:
    return MessageTemplate(
        template_id=row.id,
        direction=row.direction.value,
        occurrences=row.occurrences,
        automation_score=row.automation_score,
        sample_text=row.sample_text,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
    )


def _entry(row: MessageSummary) -> TimelineEntry:
    return TimelineEntry(
        message_id=row.id,
        sent_at=row.sent_at,
        sender=row.sender_address,
        sender_name=row.sender_name,
        subject=row.subject,
        preview=row.preview,
        labels=tuple(one.name for one in row.labels),
        has_attachments=row.has_attachments,
    )
