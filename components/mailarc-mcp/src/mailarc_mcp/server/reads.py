"""Where a tool's answer comes from — asked for on first use, never at import.

Two jobs, and the second one is a compromise worth reading before editing.

**Lazy construction.** :func:`~mailarc_mcp.server.server.build_server` has to
work in a process where nothing is configured and no graph is running: a client
asks for the tool list before it ever calls a tool, and an MCP server that could
not describe itself without a database would be undiscoverable. So every reader
below is asked for the first time a tool needs it. Holding the *factories*
rather than the readers is what buys that — an instance handed in at
construction would have had to be built before the server existed.

**Nothing here is built from configuration, and this component could not build
it.** A component may not turn settings into an object; AGENTS §6 gives that to
``app/composition.py`` alone. So the five factories arrive from outside, and in
the running application every one of them is a cached builder in the
composition root — which is what stops the Reflex process and the MCP process
holding two different embedders and giving one installation two answers to "is
semantic search available".

That is also why this class takes its readers as parameters instead of reading
module-level functions. It used to import ``app.composition`` directly, which
read correctly and made the whole server un-movable: the package could not
leave ``app/``, and the desktop bundle could not leave ``fastmcp`` behind.

**Two graph reads of its own, and both are bound the way the catalogue is.**
Most of the tools go through
:class:`~mailarc_analytics.queries.reports.AnalyticsReader`,
:class:`~mailarc_core.archive.reader.ArchiveReader`,
:class:`~mailarc_core.archive.tags.TagStore` or
:class:`~mailarc_analytics.semantic.search.SemanticSearch` and never see a
statement at all. Two do. Reading one conversation has no catalogue constant
and is written with runic's **query builder**: it compiles against the mapped
models, binds every value as a parameter, and is the path
``mailarc_core.archive.repository`` names as the house rule for graph reads —
it belongs beside the others in the catalogue and should move into
``MessageRepository`` the moment that package can be touched. Reading a topic's
members *does* have a constant, ``catalog.TOPIC_MEMBERS``, and runs it here
because no reader hands those rows out: the graph reader above it answers with
nodes and edges, which is a picture rather than something to read. What matters
for a server answering a language model holds for both — no caller-supplied
string is ever concatenated into a statement, and no tool takes Cypher.

A projection, never an entity, crosses the session boundary: runic nodes are
attached to the session that read them and lazily fetch a relation the moment
somebody touches one, so a ``Message`` handed upwards would either explode or
silently issue queries after its session was closed. The same rule
:meth:`ArchiveReader._summarise <mailarc_core.archive.reader.ArchiveReader>`
keeps.

Blocking, like every runic driver. FastMCP dispatches a synchronous tool to a
worker thread, so a blocking read here does not stall the event loop the
protocol runs on.
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager

from runic.ogm import Session, alias, select

from mailarc_analytics import AnalyticsReader
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import as_float, as_int, as_text, rows_of
from mailarc_analytics.semantic import SemanticSearch
from mailarc_core.archive.model import Address, Message, MessageSummary, Thread
from mailarc_core.archive.reader import ArchiveReader, preview_of
from mailarc_core.archive.tags import TagStore
from mailarc_mcp.server.model import (
    Conversation,
    ConversationMessage,
    TopicMessage,
    TopicMessages,
)

logger = logging.getLogger(__name__)

type GraphSessionFactory = Callable[[], AbstractContextManager[Session]]
"""Opens one session against this installation's graph. Per call, never cached
— a session is a connection, and two tools sharing one would interleave."""

type AnalyticsFactory = Callable[[], AnalyticsReader]
"""Hands back the derived layer's reader. Cheap and cached by whoever supplies
it, so a tool may ask on every call rather than holding one."""

type ArchiveFactory = Callable[[], ArchiveReader]
"""Hands back the ground truth's reader — the graph plus the blob store."""

type TagFactory = Callable[[], TagStore]
"""Hands back the annotation layer — the tags a person made, on the same graph.

The one reader here whose subject a rebuild cannot recreate: topics and circles
are recomputed and thrown away, a tag is what somebody decided. Read-only from
this process all the same — no tool on this server creates, renames or applies
one.
"""

type SearchFactory = Callable[[], SemanticSearch]
"""Hands back both search paths, with whatever embedder is configured or none.

The one factory whose result must be shared across calls: the embedder holds an
``httpx`` connection pool, and a fresh one per query pays a TLS handshake for
nothing. The caller owns that decision, which in the application means
``app.composition.semantic_search`` is ``lru_cache``d.
"""


MESSAGE = alias(Message, "m")
THREAD = alias(Thread, "t")
"""The two named variables these reads share.

runic 0.5 replaced ``.alias("m")`` chaining with handles, and with it the
``_field()`` detour this module used to carry: a handle's attribute is a typed
expression, so ``MESSAGE.id.in_(keys)`` type-checks where
``Message.id.in_(keys)`` does not, and ``MESSAGE.thread`` supplies the relation
*and* anchors the pattern to ``m``. A renamed field still fails at import.
"""


class ArchiveAccess:
    """Everything the tools read through, gathered behind five factories.

    A plain class and not a ``Protocol``: there is one implementation, and the
    tests subclass it to answer from a graph of their own. A protocol here
    would be indirection with nothing on the other side of it.

    Nothing is built here and nothing is called here — the arguments are
    stored and asked the first time a tool needs one. Construction therefore
    opens no connection and reads no configuration, which is the property that
    lets a client list this server's tools against a machine whose archive is
    not running.

    Keyword-only, all five, because they are zero-argument callables that
    differ in nothing a positional call could show. Required, all five, because
    a default would be this component building itself from configuration.
    """

    def __init__(
        self,
        *,
        graph_session: GraphSessionFactory,
        analytics: AnalyticsFactory,
        archive: ArchiveFactory,
        search: SearchFactory,
        tags: TagFactory,
    ) -> None:
        self._graph_session = graph_session
        self._analytics = analytics
        self._archive = archive
        self._search = search
        self._tags = tags

    def session(self) -> AbstractContextManager[Session]:
        """One graph session, opened against this installation's server."""
        return self._graph_session()

    def analytics(self) -> AnalyticsReader:
        """The derived layer: correspondents, topics, templates."""
        return self._analytics()

    def archive(self) -> ArchiveReader:
        """The ground truth: what was imported, newest first."""
        return self._archive()

    def search(self) -> SemanticSearch:
        """Both search paths, with whatever embedder is configured — or none.

        Asked for per call and expected to answer with the same object every
        time. The embedder behind it holds an ``httpx`` connection pool: one
        torn down per call would pay a TLS handshake for every query, and one
        closed per client session would leave the *next* session on this
        process talking to a closed pool. It goes when the process goes, which
        for a stdio server is when the client stops it.
        """
        return self._search()

    def tags(self) -> TagStore:
        """The annotation layer: the tags a person put on their own mail."""
        return self._tags()

    def conversation(self, message_id: str, limit: int) -> Conversation | None:
        """The messages threaded with this one, oldest first.

        ``None`` when the archive holds no message under that id — the caller
        turns that into an error a model can act on, because an empty
        conversation would read as "this message stands alone", which is a
        different and legitimate answer.

        A message with no ``IN_THREAD`` edge is exactly that legitimate answer
        and comes back as a conversation of one: providers without threading,
        and any mail nobody ever replied to, land here.

        One extra row is asked for beyond ``limit`` so that :attr:`Conversation.
        truncated` can be answered without a second count — a caller that is
        told nothing about the tail assumes it read the whole exchange.
        """
        with self.session() as graph:
            anchor: Message | None = graph.get(Message, message_id, fetch=["thread"])
            if anchor is None:
                return None
            thread = anchor.thread
            if thread is None:
                return Conversation(
                    subject=anchor.subject or "",
                    messages=(_message_of(anchor, _sender_of(graph, anchor)),),
                )
            members = _thread_members(graph, thread.id, limit + 1)
            truncated = len(members) > limit
            members = members[:limit]
            senders = _senders_of(graph, [one.id for one in members])
            return Conversation(
                thread_id=thread.id,
                subject=thread.subject or (members[0].subject if members else "") or "",
                messages=tuple(
                    _message_of(one, senders.get(one.id)) for one in members
                ),
                truncated=truncated,
            )

    def topic(self, topic_id: str, limit: int) -> TopicMessages | None:
        """The mail one topic is made of, the most important message first.

        ``None`` when the graph holds no topic under that id — and after a
        rebuild that is the *ordinary* reason rather than a mistake: a
        ``Topic.id`` is a digest of its members and is a different string every
        time the derived layer is computed. The caller turns it into a sentence
        saying so, because "no such topic" on its own reads as "that piece of
        work does not exist".

        Two reads, and each does what only it can. The catalogue statement
        orders the members by ``Message.importance`` and says how many the
        topic holds; :meth:`ArchiveReader.messages_by_ids
        <mailarc_core.archive.reader.ArchiveReader.messages_by_ids>` turns the
        ids into rows — it is the one place that knows how to show a message,
        and a preview assembled here instead would be a second copy of that
        knowledge drifting from the first.

        **Members are deduplicated.** A message joined to the same topic by two
        signals wears two ``ABOUT`` edges and comes back on two rows; a listing
        that repeated it would spend a model's context saying the same thing
        twice. That also means the cut can be shorter than the limit asked for,
        which is what :attr:`TopicMessages.truncated` is read off the topic's
        own count for rather than off the number of rows.
        """
        with self.session() as graph:
            rows = rows_of(
                graph, catalog.TOPIC_MEMBERS, {"topic": topic_id, "limit": limit}
            )
        if not rows:
            return None
        scores: dict[str, float] = {}
        for row in rows:
            key = as_text(row.get("id"))
            if key and key not in scores:
                scores[key] = as_float(row.get("importance"))
        found = self.archive().messages_by_ids(list(scores))
        return TopicMessages(
            topic_id=topic_id,
            label=as_text(rows[0].get("topic_label")),
            messages=tuple(_member_of(one, scores) for one in found),
            truncated=as_int(rows[0].get("topic_messages")) > len(found),
        )


def _member_of(summary: MessageSummary, scores: Mapping[str, float]) -> TopicMessage:
    """One topic member as the wire carries it, with the score that ordered it.

    The score comes off the statement's row and not off the summary, because
    ``MessageSummary`` is the *import's* view of a message and importance is a
    derived property the rebuild writes — a listing that had to carry it would
    put a derived number in every answer the archive gives about a message.
    """
    return TopicMessage(
        message_id=summary.id,
        subject=summary.subject,
        sender=summary.sender_address,
        sent_at=summary.sent_at,
        preview=summary.preview,
        importance=scores.get(summary.id, 0.0),
    )


def _thread_members(session: Session, thread_id: str, limit: int) -> list[Message]:
    """The messages hanging off one ``Thread`` node, oldest first.

    Filtered on the *traversed* alias rather than on a property of the message,
    because a message carries no thread id: the conversation is an edge, which
    is what makes "same conversation" a fact about the provider's grouping
    instead of a guess about two subject lines.

    ``optional=False`` — an ordinary ``MATCH`` — so a message that is in no
    thread cannot appear here with a null on the other side.
    """
    statement = (
        select(MESSAGE)
        .traverse(MESSAGE.thread, to=THREAD, optional=False)
        .where(THREAD.id == thread_id)
        .order_by(MESSAGE.sent_at)
        .limit(limit)
        .return_target(MESSAGE)
    )
    return session.scalars(statement)


def _senders_of(session: Session, ids: Iterable[str]) -> dict[str, Address]:
    """The sending address of each of these messages, keyed by message id.

    A second statement rather than a second traversal on the listing above:
    two expansions off one node multiply rows, so a conversation's ``LIMIT``
    would start counting *pairs* instead of messages the first time a message
    had more than one edge. The same split
    :meth:`MessageRepository.find_labels
    <mailarc_core.archive.repository.MessageRepository>` makes, for the same
    reason. A message whose ``From`` could not be parsed is simply absent.
    """
    keys = list(ids)
    if not keys:
        return {}
    statement = (
        select(MESSAGE)
        .where(MESSAGE.id.in_(keys))
        .traverse(MESSAGE.sender, to="s", optional=False)
        .return_nodes(MESSAGE, "s")
    )
    found: dict[str, Address] = {}
    for message, sender in session.all_with_edges(statement):
        found[message.id] = sender
    return found


def _sender_of(session: Session, message: Message) -> Address | None:
    """One message's sender — the single-row case of :func:`_senders_of`."""
    return _senders_of(session, [message.id]).get(message.id)


def _message_of(message: Message, sender: Address | None) -> ConversationMessage:
    """One node as the conversation shows it. Called inside the session, always.

    Reading a runic entity's fields after its session has closed is what this
    projection exists to prevent: an unloaded relation would go looking for a
    driver that is gone.
    """
    return ConversationMessage(
        message_id=message.id,
        subject=message.subject or "",
        sender=sender.id if sender is not None else "",
        sent_at=message.sent_at,
        preview=preview_of(message.body_clean or message.body_text),
    )
