"""Where a tool's answer comes from — asked for on first use, never at import.

Two jobs, and the second one is a compromise worth reading before editing.

**Lazy construction.** :func:`~mailarc_mcp.server.server.build_server` has to
work in a process where nothing is configured and no graph is running: a client
asks for the tool list before it ever calls a tool, and an MCP server that could
not describe itself without a database would be undiscoverable. So every reader
below is asked for the first time a tool needs it. Holding the four *factories*
rather than the four readers is what buys that — an instance handed in at
construction would have had to be built before the server existed.

**Nothing here is built from configuration, and this component could not build
it.** A component may not turn settings into an object; AGENTS §6 gives that to
``app/composition.py`` alone. So the four factories arrive from outside, and in
the running application every one of them is a cached builder in the
composition root — which is what stops the Reflex process and the MCP process
holding two different embedders and giving one installation two answers to "is
semantic search available".

That is also why this class takes four parameters instead of reading four
module-level functions. It used to import ``app.composition`` directly, which
read correctly and made the whole server un-movable: the package could not
leave ``app/``, and the desktop bundle could not leave ``fastmcp`` behind.

**One graph read that the catalogue does not have.** Five of the six tools go
through :class:`~mailarc_analytics.queries.reports.AnalyticsReader`,
:class:`~mailarc_core.archive.reader.ArchiveReader` or
:class:`~mailarc_analytics.semantic.search.SemanticSearch`, and every statement
they run is a named constant in ``mailarc_analytics.queries.catalog``. The
sixth — read one conversation — has no such constant, and this phase may not
add one. It is written with runic's **query builder** instead: the builder
compiles against the mapped models, binds every value as a parameter, and is
the path ``mailarc_core.archive.repository`` names as the house rule for graph
reads. What matters for a server that answers a language model is unchanged —
no caller-supplied string is ever concatenated into a statement, and no tool
takes Cypher — but this read belongs beside the others in the catalogue, and
should move into ``MessageRepository`` the moment that package can be touched.

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
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager

from runic.ogm import Session, alias, select

from mailarc_analytics import AnalyticsReader
from mailarc_analytics.semantic import SemanticSearch
from mailarc_core.archive.model import Address, Message, Thread
from mailarc_core.archive.reader import ArchiveReader, preview_of
from mailarc_mcp.server.model import Conversation, ConversationMessage

logger = logging.getLogger(__name__)

type GraphSessionFactory = Callable[[], AbstractContextManager[Session]]
"""Opens one session against this installation's graph. Per call, never cached
— a session is a connection, and two tools sharing one would interleave."""

type AnalyticsFactory = Callable[[], AnalyticsReader]
"""Hands back the derived layer's reader. Cheap and cached by whoever supplies
it, so a tool may ask on every call rather than holding one."""

type ArchiveFactory = Callable[[], ArchiveReader]
"""Hands back the ground truth's reader — the graph plus the blob store."""

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
    """Everything the tools read through, gathered behind four factories.

    A plain class and not a ``Protocol``: there is one implementation, and the
    tests subclass it to answer from a graph of their own. A protocol here
    would be indirection with nothing on the other side of it.

    Nothing is built here and nothing is called here — the four arguments are
    stored and asked the first time a tool needs one. Construction therefore
    opens no connection and reads no configuration, which is the property that
    lets a client list this server's tools against a machine whose archive is
    not running.

    Keyword-only, all four, because they are four zero-argument callables that
    differ in nothing a positional call could show. Required, all four, because
    a default would be this component building itself from configuration.
    """

    def __init__(
        self,
        *,
        graph_session: GraphSessionFactory,
        analytics: AnalyticsFactory,
        archive: ArchiveFactory,
        search: SearchFactory,
    ) -> None:
        self._graph_session = graph_session
        self._analytics = analytics
        self._archive = archive
        self._search = search

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
