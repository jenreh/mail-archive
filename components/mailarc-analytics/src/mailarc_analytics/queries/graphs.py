"""Asking the archive what one corner of it looks like — the subgraph façade.

:class:`~mailarc_analytics.queries.reports.AnalyticsReader`'s sibling, built the
same way and for the same reason: a session factory and nothing else, numbers
and ids in, frozen value objects out, and not a character of Cypher anywhere in
the file. What it answers is different — a *picture* rather than a listing —
and that difference is the whole of what this module decides.

Seven views and two ways round them. :meth:`~GraphReader.topic`,
:meth:`~GraphReader.message`, :meth:`~GraphReader.address`,
:meth:`~GraphReader.tag`, :meth:`~GraphReader.community` and
:meth:`~GraphReader.overview` each run a handful of the statements in
:mod:`mailarc_analytics.queries.statements.graph` **in one session** and fold
the rows into one :class:`~mailarc_analytics.queries.model.Subgraph`;
:meth:`~GraphReader.path` answers "how are these two connected" out of the
store's own shortest paths; :meth:`~GraphReader.expand` is one hop out of a node
somebody double-clicked, for the caller to merge onto what is already drawn.

What the reader does that no statement can
------------------------------------------

**Degree is counted here, and it has to be.** A node's degree in the *archive*
is not the number a canvas can size by — the picture holds a dozen of a
correspondent's four thousand edges — so the only honest degree is the one over
the edges this subgraph actually drew, which exists nowhere until the rows are
folded together.

**Every weight is normalised within the subgraph.** A canvas maps a weight onto
a diameter between two pixel sizes, so the scale has to be the picture: the
heaviest node in this view is 1.0 and everything else is a fraction of it. An
importance of 0.4 means "middling" in an archive and "the smaller of the two"
in a view of two messages, and it is the second reading a drawing can show.
A node that has no such number keeps no key at all, because a missing weight
and a weight of zero must not be drawn the same.

**A dangling edge is dropped.** Every listing is cut at a limit, so an overlap
between two collections can name a topic the overview stopped short of, and
cytoscape throws on an edge with an end it was never given. The two guarantees
:class:`~mailarc_analytics.queries.model.Subgraph` makes — both ends present,
no id twice — are established in :meth:`_Assembly.finish` and nowhere else.

**The depth cut is a walk, not a slice.** ``REPLY_CHAIN`` walks a fixed
``*1..3`` because a variable-length bound is Cypher syntax rather than a value,
so the depth a user picks is applied here, in hops from the seed over the edges
that came back. Hops and not arrows: a message that answered the seed is as
near as one the seed answered.

``algo.BFS`` is *not* what expansion uses, although
:data:`~mailarc_analytics.queries.statements.algorithms.NEIGHBOURHOOD` is right
there. It yields ``nodes`` and ``edges`` as two flat lists and no endpoints, so
what comes back is a bag of ids and a bag of type names with nothing saying
which joins which — enough to count a neighbourhood, not enough to draw one.
The typed reads below cost one round trip each and know what they are looking
at.

Synchronous, because every runic driver blocks; a Reflex background handler
wraps a call in ``asyncio.to_thread`` the way the insights page already does.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.model import (
    GraphEdge,
    GraphNode,
    NodeKind,
    Subgraph,
    Weight,
)
from mailarc_analytics.queries.rows import as_float, as_text, rows_of
from mailarc_core.archive.reader import GraphSessionFactory

logger = logging.getLogger(__name__)

GRAPH_LIMIT = 100
"""Rows each read of a view returns unless the caller says otherwise.

A canvas and not a table, which is why this is half of
:data:`~mailarc_analytics.queries.reports.REPORT_LIMIT` rather than a multiple
of it: five hundred nodes in a force layout is a hairball nobody can point at,
and every one of these reads is ordered by the number that matters, so the
first hundred are the hundred worth drawing.
"""

MAX_GRAPH_ROWS = 1_000
"""The most rows any one read of a view may be asked for.

A ceiling on the picture rather than on the archive — an explorer that pulled
ten thousand nodes into a browser would hang the tab it was drawing in — and
this module is a public surface, so the number a caller passes is one this
repository does not choose.
"""

MAX_DEPTH = 3
"""How far :meth:`GraphReader.message` can follow a reply chain.

Three, because that is what
:data:`~mailarc_analytics.queries.statements.graph.REPLY_CHAIN` walks: a
variable-length quantifier is Cypher syntax and not a bound value, so a deeper
walk is a promise this reader cannot keep by binding anything.
"""

PATH_LENGTH = 4
"""Hops :meth:`GraphReader.path` looks for a connection within, by default.

Four steps of "these two were written to together" is a claim a reader can
still follow; past that the answer is "everybody is connected to everybody",
which is true of any address book and worth nothing.
"""

MAX_PATH_LENGTH = 8
"""The longest walk a caller may ask :meth:`GraphReader.path` for."""

PATH_COUNT = 3
"""Shortest paths the store is asked for between two addresses.

More than one because the interesting answer to "how do these two know each
other" is often the second route, and a small number because they are drawn on
top of each other.
"""

PARTICIPATES = "PARTICIPATES"
"""An address is on some of a collection's mail — an edge this reader coins.

Not a relationship type: nothing in the graph joins a person to a topic. What
the statement counts is the messages of the collection that address is on, and
one weighted line per person is what a canvas wants where the join itself is
``members × people`` lines through the mail.
"""

OVERLAPS = "OVERLAPS"
"""Two collections share some mail — the overview's only edge, also coined.

Same argument as :data:`PARTICIPATES`, one level up: a topic and a circle meet
through the messages that are in both, and the map wants the number rather than
the path.
"""


class GraphReader:
    """The graph as a canvas needs it — one corner at a time.

    Read-only, like the analytics reader and more so: nothing here writes, and
    nothing here can be handed Cypher. Every method takes ids and numbers, and
    the statements are named constants reached as ``catalog.SOMETHING``.

    One session per view, not per statement. Six reads of one message through
    six drivers would be six connections for one picture, and — worse — six
    moments: a rebuild running underneath would put a topic in the picture that
    the message is no longer in.
    """

    def __init__(self, graph_session: GraphSessionFactory) -> None:
        self._graph_session = graph_session

    def message(
        self, message_id: str, *, depth: int = 1, limit: int = GRAPH_LIMIT
    ) -> Subgraph:
        """One message and everything the graph says about it.

        Six reads: who is on it, the rest of its provider thread, the replies
        around it, and the three derived things it belongs to. The message
        itself is drawn only if at least one of them answered — a graph that
        has never heard of this id gets an empty subgraph rather than a lonely
        node claiming a message exists.

        *depth* cuts the reply chain and nothing else; see this module's
        docstring for why the cut happens here.
        """
        asked = _limit(limit)
        with self._graph_session() as graph:
            addresses = rows_of(graph, catalog.MESSAGE_ADDRESSES, {"id": message_id})
            siblings = _seeded(graph, catalog.THREAD_SIBLINGS, message_id, asked)
            chain = rows_of(
                graph, catalog.REPLY_CHAIN, {"id": message_id, "limit": asked}
            )
            topics = rows_of(graph, catalog.MESSAGE_TOPICS, {"id": message_id})
            tags = rows_of(graph, catalog.MESSAGE_TAGS, {"id": message_id})
            circles = rows_of(graph, catalog.MESSAGE_CIRCLE, {"id": message_id})
        found = _Assembly(asked)
        found.cut("siblings", siblings.rows, siblings.asked)
        found.cut("replies", chain, asked)
        if any((addresses, siblings.rows, chain, topics, tags, circles)):
            found.node(
                message_id,
                NodeKind.MESSAGE,
                _first(addresses, "message_subject"),
                weights=_score_of(_first_row(addresses) or {}, "message_importance"),
            )
        _addresses_of(found, message_id, addresses)
        _thread_of(found, siblings.rows)
        _reply_chain(found, message_id, chain, _depth(depth))
        _filings_of(found, message_id, topics, tags, circles)
        return found.finish()

    def topic(self, topic_id: str, *, limit: int = GRAPH_LIMIT) -> Subgraph:
        """One piece of work: its mail, and the people on that mail.

        Two reads and two stars round one hub — the ``ABOUT`` edges a rebuild
        wrote, and the participation this reader counts. An empty answer is R7's
        case: a topic id is a digest of its members and is minted afresh by
        every rebuild, so a bookmarked link goes stale and the page says so.
        """
        asked = _limit(limit)
        with self._graph_session() as graph:
            members = _seeded(graph, catalog.TOPIC_MEMBERS, topic_id, asked, "topic")
            people = _seeded(
                graph, catalog.TOPIC_PARTICIPANTS, topic_id, asked, "topic"
            )
        found = _Assembly(asked)
        found.cut("members", members.rows, asked)
        found.cut("participants", people.rows, asked)
        if members.rows or people.rows:
            found.node(
                topic_id,
                NodeKind.TOPIC,
                _first(members.rows, "topic_label"),
                weights=_count_of(members.rows, "topic_messages"),
            )
        _messages_under(found, topic_id, members.rows, "ABOUT", "method")
        _people_around(found, topic_id, people.rows)
        return found.finish()

    def address(self, address_id: str, *, limit: int = GRAPH_LIMIT) -> Subgraph:
        """One correspondent: their mail, and who they are written to with."""
        asked = _limit(limit)
        with self._graph_session() as graph:
            mail = _seeded(
                graph, catalog.ADDRESS_MESSAGES, address_id, asked, "address"
            )
            near = _seeded(
                graph, catalog.ADDRESS_NEIGHBOURS, address_id, asked, "address"
            )
        found = _Assembly(asked)
        found.cut("messages", mail.rows, asked)
        found.cut("neighbours", near.rows, asked)
        if mail.rows or near.rows:
            found.node(
                address_id,
                NodeKind.ADDRESS,
                address_id,
                weights=_rank_of(_first_row(mail.rows), "address_rank"),
                props=_domain_of(_first_row(mail.rows), "address_domain"),
            )
        for row in mail.rows:
            _message_node(found, row)
            found.edge(as_text(row["id"]), address_id, as_text(row["kind"]))
        for row in near.rows:
            _address_node(found, row)
            found.edge(
                address_id,
                as_text(row["id"]),
                "CO_ADDRESSED",
                weight=as_float(row["together"]),
            )
        return found.finish()

    def tag(self, tag_id: str, *, limit: int = GRAPH_LIMIT) -> Subgraph:
        """One tag and the mail a person filed under it."""
        asked = _limit(limit)
        with self._graph_session() as graph:
            members = _seeded(graph, catalog.TAG_MEMBERS, tag_id, asked, "tag")
        found = _Assembly(asked)
        found.cut("members", members.rows, asked)
        if members.rows:
            found.node(tag_id, NodeKind.TAG, _first(members.rows, "tag_name"))
        _messages_under(found, tag_id, members.rows, "TAGGED", "source")
        return found.finish()

    def community(self, community_id: str, *, limit: int = GRAPH_LIMIT) -> Subgraph:
        """One circle: the people in it and the mail that circulates inside."""
        asked = _limit(limit)
        name = "community"
        with self._graph_session() as graph:
            people = _seeded(
                graph, catalog.COMMUNITY_MEMBERS, community_id, asked, name
            )
            mail = _seeded(graph, catalog.COMMUNITY_MESSAGES, community_id, asked, name)
        found = _Assembly(asked)
        found.cut("members", people.rows, asked)
        found.cut("messages", mail.rows, asked)
        if people.rows or mail.rows:
            found.node(
                community_id,
                NodeKind.COMMUNITY,
                _first(people.rows, "community_label"),
            )
        for row in people.rows:
            _address_node(found, row, rank="rank")
            found.edge(
                as_text(row["id"]),
                community_id,
                "MEMBER_OF",
                weight=as_float(row["rank"]),
            )
        for row in mail.rows:
            _message_node(found, row)
            found.edge(
                as_text(row["id"]),
                community_id,
                "IN_CIRCLE",
                weight=as_float(row["score"]),
            )
        return found.finish()

    def overview(self, *, limit: int = GRAPH_LIMIT) -> Subgraph:
        """The archive at one remove: its collections, and where they overlap.

        Five reads and no messages at all. The mail is what the collections are
        joined *through*, and drawing it would put the whole archive on the
        canvas to say that a project and a circle share four letters.

        An overlap naming a collection the listings stopped short of is dropped
        rather than stubbed in — see :meth:`_Assembly.finish`.
        """
        asked = _limit(limit)
        with self._graph_session() as graph:
            topics = _listing(graph, catalog.OVERVIEW_TOPICS, asked)
            circles = _listing(graph, catalog.OVERVIEW_COMMUNITIES, asked)
            tags = _listing(graph, catalog.OVERVIEW_TAGS, asked)
            shared = _listing(graph, catalog.OVERVIEW_TOPIC_CIRCLE, asked)
            filed = _listing(graph, catalog.OVERVIEW_TAG_TOPIC, asked)
        found = _Assembly(asked)
        found.cut("topics", topics, asked)
        found.cut("circles", circles, asked)
        found.cut("tags", tags, asked)
        for row in topics:
            _collection(found, row, NodeKind.TOPIC, "label", "message_count")
        for row in circles:
            _collection(found, row, NodeKind.COMMUNITY, "label", "message_count")
        for row in tags:
            _collection(found, row, NodeKind.TAG, "name", "messages")
        for row in shared:
            found.edge(
                as_text(row["topic_id"]),
                as_text(row["community_id"]),
                OVERLAPS,
                weight=as_float(row["messages"]),
            )
        for row in filed:
            found.edge(
                as_text(row["tag_id"]),
                as_text(row["topic_id"]),
                OVERLAPS,
                weight=as_float(row["messages"]),
            )
        return found.finish()

    def path(self, left: str, right: str, *, max_len: int = PATH_LENGTH) -> Subgraph:
        """How two correspondents are connected, in as few hops as there are.

        Over ``CO_ADDRESSED`` and in both directions, which is what makes the
        answer readable: a path through the messages themselves would alternate
        person, mail, person and report twice the hop count a human would call
        it.

        **The picture holds more than the shortest route.**
        :data:`~mailarc_analytics.queries.statements.algorithms.SHORTEST_PATHS`
        asked for :data:`PATH_COUNT` paths answers with that many *shortest*
        ones rather than with copies of the best — measured on the planted
        corpus, where two people who share a message come back with the direct
        edge and with the way round through the archive's owner. That is the
        answer "how else are these two connected" wants, and it is why the count
        is three rather than one.

        Two people with nothing between them get an empty subgraph — the
        procedure answers with no rows rather than raising, so a stale id from
        a bookmark reads the same as a genuine absence of any connection.
        """
        walk = min(max(1, max_len), MAX_PATH_LENGTH)
        with self._graph_session() as graph:
            paths = rows_of(
                graph,
                catalog.SHORTEST_PATHS,
                {
                    "left": left,
                    "right": right,
                    "max_len": walk,
                    "path_count": PATH_COUNT,
                },
            )
        found = _Assembly(PATH_COUNT)
        found.cut("routes", paths, PATH_COUNT)
        for row in paths:
            stops = [as_text(one) for one in _listed(row["ids"])]
            steps = [as_text(one) for one in _listed(row["types"])]
            for stop in stops:
                found.node(stop, NodeKind.ADDRESS, stop)
            for index, kind in enumerate(steps[: len(stops) - 1]):
                found.edge(stops[index], stops[index + 1], kind)
        return found.finish()

    def expand(
        self, node_id: str, kind: NodeKind, *, limit: int = GRAPH_LIMIT
    ) -> Subgraph:
        """One hop out of a node, for the caller to lay over what is drawn.

        The same views, asked about one node — an expansion is not a different
        question from "show me this", it is the answer put beside another one.
        A message expands to depth one whatever the explorer's depth setting
        says, because a double-click is a hop.

        A thread has no read of its own and answers with an empty subgraph
        rather than an error: it is a hub the message view draws, and every
        message on it is already there.
        """
        if kind is NodeKind.MESSAGE:
            return self.message(node_id, depth=1, limit=limit)
        if kind is NodeKind.TOPIC:
            return self.topic(node_id, limit=limit)
        if kind is NodeKind.ADDRESS:
            return self.address(node_id, limit=limit)
        if kind is NodeKind.TAG:
            return self.tag(node_id, limit=limit)
        if kind is NodeKind.COMMUNITY:
            return self.community(node_id, limit=limit)
        logger.debug("Nothing to expand from a %s node", kind.value)
        return Subgraph()


class _Assembly:
    """Rows going in, one drawable subgraph coming out.

    Deduplicating, degree-counting and normalising in one place, because all
    three are properties of the *picture* rather than of any read that went into
    it. Every view builds one, feeds it whatever its statements answered and
    asks it for the result.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str], GraphEdge] = {}
        self._cut: set[str] = set()

    def node(
        self,
        node_id: str,
        kind: NodeKind,
        label: str = "",
        *,
        weights: Mapping[str, float] | None = None,
        props: Mapping[str, str] | None = None,
    ) -> None:
        """Add a node, or fill in what an earlier reading of it did not know.

        A message reached as a thread sibling and again as a reply is one node
        with both of its edges — a canvas refuses a duplicate id outright — and
        the reading that knew its subject is the one that gets to name it.
        """
        found = self._nodes.get(node_id)
        if found is None:
            self._nodes[node_id] = GraphNode(
                id=node_id,
                kind=kind,
                label=label,
                weights=dict(weights or {}),
                props=dict(props or {}),
            )
            return
        self._nodes[node_id] = found.model_copy(
            update={
                "label": found.label or label,
                "weights": {**dict(weights or {}), **found.weights},
                "props": {**dict(props or {}), **found.props},
            }
        )

    def edge(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        weight: float = 0.0,
        label: str = "",
    ) -> None:
        """Add a line, keeping the heavier of two readings of the same one."""
        key = (source, target, kind)
        found = self._edges.get(key)
        if found is not None and found.weight >= weight:
            return
        self._edges[key] = GraphEdge(
            source=source, target=target, kind=kind, weight=weight, label=label
        )

    def cut(self, what: str, rows: Sequence[Any], limit: int) -> None:
        """Note that *what* came back full, which means it was cut.

        A read that filled its limit exactly is indistinguishable from one that
        was truncated, and the honest reading of the ambiguity is the pessimistic
        one: a user told the picture is complete when it is not has no reason to
        look for what is missing.
        """
        if limit > 0 and len(rows) >= limit:
            self._cut.add(what)

    def finish(self) -> Subgraph:
        """The subgraph, with its guarantees established.

        Three passes and each is this module's docstring in code: drop the edges
        whose ends were cut off a listing, count the degree over what is left,
        and normalise every weight within the picture.
        """
        edges = tuple(
            one
            for one in self._edges.values()
            if one.source in self._nodes and one.target in self._nodes
        )
        dropped = len(self._edges) - len(edges)
        if dropped:
            logger.debug("Left out %d edge(s) with an end outside the view", dropped)
        degrees: dict[str, float] = {}
        for one in edges:
            degrees[one.source] = degrees.get(one.source, 0.0) + 1
            degrees[one.target] = degrees.get(one.target, 0.0) + 1
        weighted = {
            node_id: {**one.weights, Weight.DEGREE.value: degrees.get(node_id, 0.0)}
            for node_id, one in self._nodes.items()
        }
        scaled = _normalised(weighted)
        return Subgraph(
            nodes=tuple(
                one.model_copy(update={"weights": scaled[node_id]})
                for node_id, one in self._nodes.items()
            ),
            edges=edges,
            truncated=bool(self._cut),
            notice=self._notice(),
        )

    def _notice(self) -> str:
        """One sentence naming every read that hit the ceiling."""
        if not self._cut:
            return ""
        return f"Cut to the first {self._limit}: {', '.join(sorted(self._cut))}."


class _Rows:
    """One read's rows and the ceiling they were asked for under."""

    def __init__(self, rows: list[dict[str, Any]], asked: int) -> None:
        self.rows = rows
        self.asked = asked


def _seeded(
    session: Session,
    statement: Statement,
    seed: str,
    limit: int,
    parameter: str = "id",
) -> _Rows:
    """Run one of the seeded, limited reads and remember what it was asked."""
    return _Rows(rows_of(session, statement, {parameter: seed, "limit": limit}), limit)


def _listing(
    session: Session, statement: Statement, limit: int
) -> list[dict[str, Any]]:
    """Run one of the overview's reads, which take a ceiling and nothing else."""
    return rows_of(session, statement, {"limit": limit})


def _addresses_of(
    found: _Assembly, message_id: str, rows: Sequence[Mapping[str, Any]]
) -> None:
    """The people on one message, each on the line the header put them."""
    for row in rows:
        _address_node(found, row)
        found.edge(message_id, as_text(row["id"]), as_text(row["kind"]))


def _thread_of(found: _Assembly, rows: Sequence[Mapping[str, Any]]) -> None:
    """The provider's conversation as a hub its members hang off."""
    for row in rows:
        thread = as_text(row["thread_id"])
        found.node(thread, NodeKind.THREAD, as_text(row["thread_subject"]))
        _message_node(found, row)
        found.edge(as_text(row["id"]), thread, "IN_THREAD")


def _reply_chain(
    found: _Assembly,
    message_id: str,
    rows: Sequence[Mapping[str, Any]],
    depth: int,
) -> None:
    """Who answered whom, cut to *depth* hops from the seed.

    The rows are whole paths, so the same edge arrives once per path that walks
    it. What is drawn is the union, walked outwards from the seed: a message the
    chain only reaches through another one is a hop further out, whichever path
    it was on.
    """
    known: dict[str, Mapping[str, Any]] = {}
    edges: list[tuple[str, str]] = []
    for row in rows:
        ids = [as_text(one) for one in _listed(row["ids"])]
        subjects = _listed(row["subjects"])
        scores = _listed(row["importances"])
        for index, one in enumerate(ids):
            known.setdefault(
                one,
                {
                    "subject": _at(subjects, index),
                    "importance": _at(scores, index),
                },
            )
        sources = [as_text(one) for one in _listed(row["sources"])]
        targets = [as_text(one) for one in _listed(row["targets"])]
        edges.extend(zip(sources, targets, strict=False))
    near = _within(message_id, edges, depth)
    for one in sorted(near):
        _message_node(found, {"id": one, **known.get(one, {})})
    for source, target in edges:
        if source in near and target in near:
            found.edge(source, target, "REPLIES_TO")


def _filings_of(
    found: _Assembly,
    message_id: str,
    topics: Sequence[Mapping[str, Any]],
    tags: Sequence[Mapping[str, Any]],
    circles: Sequence[Mapping[str, Any]],
) -> None:
    """The three derived things a message belongs to, each as its own kind."""
    for row in topics:
        found.node(
            as_text(row["id"]),
            NodeKind.TOPIC,
            as_text(row["label"]),
            weights=_count_of([row], "message_count"),
        )
        found.edge(
            message_id, as_text(row["id"]), "ABOUT", label=as_text(row["method"])
        )
    for row in tags:
        found.node(as_text(row["id"]), NodeKind.TAG, as_text(row["name"]))
        found.edge(message_id, as_text(row["id"]), "TAGGED")
    for row in circles:
        found.node(
            as_text(row["id"]),
            NodeKind.COMMUNITY,
            as_text(row["label"]),
            weights=_count_of([row], "message_count"),
        )
        found.edge(
            message_id,
            as_text(row["id"]),
            "IN_CIRCLE",
            weight=as_float(row["score"]),
        )


def _messages_under(
    found: _Assembly,
    hub: str,
    rows: Sequence[Mapping[str, Any]],
    kind: str,
    label: str,
) -> None:
    """A collection's mail, hung off it by the edge the store holds."""
    for row in rows:
        _message_node(found, row)
        found.edge(as_text(row["id"]), hub, kind, label=as_text(row[label]))


def _people_around(
    found: _Assembly, hub: str, rows: Sequence[Mapping[str, Any]]
) -> None:
    """The participants of a collection's mail, as one weighted line each."""
    for row in rows:
        _address_node(found, row)
        found.edge(
            as_text(row["id"]),
            hub,
            PARTICIPATES,
            weight=as_float(row["messages"]),
        )


def _collection(
    found: _Assembly,
    row: Mapping[str, Any],
    kind: NodeKind,
    label: str,
    count: str,
) -> None:
    """One of the overview's nodes — a topic, a circle or a tag."""
    found.node(
        as_text(row["id"]),
        kind,
        as_text(row[label]),
        weights={Weight.COUNT.value: as_float(row[count])},
    )


def _message_node(found: _Assembly, row: Mapping[str, Any]) -> None:
    """One message node out of any read that carries a subject and a score."""
    found.node(
        as_text(row["id"]),
        NodeKind.MESSAGE,
        as_text(row.get("subject")),
        weights=_score_of(row, "importance"),
        props=_sent_at_of(row),
    )


def _address_node(found: _Assembly, row: Mapping[str, Any], rank: str = "rank") -> None:
    """One address node. The address is its own label — there is no better."""
    found.node(
        as_text(row["id"]),
        NodeKind.ADDRESS,
        as_text(row["id"]),
        weights=_rank_of(row, rank),
        props=_domain_of(row, "domain"),
    )


def _within(seed: str, edges: Sequence[tuple[str, str]], depth: int) -> frozenset[str]:
    """The ids no more than *depth* hops from *seed*, arrows ignored.

    A breadth-first walk rather than a slice of each path, because the same
    message can be one hop away on one path and three on another, and the near
    reading is the true one.
    """
    adjacency: dict[str, set[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    seen = {seed}
    edge_of_the_walk = {seed}
    for _ in range(depth):
        further = {
            one
            for node in edge_of_the_walk
            for one in adjacency.get(node, ())
            if one not in seen
        }
        seen |= further
        edge_of_the_walk = further
    return frozenset(seen)


def _normalised(weights: Mapping[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Every weight as a fraction of the largest one in this picture.

    Per key and never across keys: an importance of 0.8 and a degree of four are
    not comparable, and a canvas sizes by one of them at a time. A key nothing
    carries a positive value for comes out zero rather than dividing by nothing.
    """
    ceilings: dict[str, float] = {}
    for one in weights.values():
        for name, value in one.items():
            ceilings[name] = max(ceilings.get(name, 0.0), value)
    return {
        node_id: {
            name: value / ceilings[name] if ceilings[name] > 0 else 0.0
            for name, value in one.items()
        }
        for node_id, one in weights.items()
    }


def _first(rows: Sequence[Mapping[str, Any]], column: str) -> str:
    """A seed's own column, off the first row that carries it."""
    row = _first_row(rows)
    return "" if row is None else as_text(row.get(column))


def _first_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return rows[0] if rows else None


def _count_of(rows: Sequence[Mapping[str, Any]], column: str) -> dict[str, float]:
    """A collection's own size as a weight, where the node carries one."""
    row = _first_row(rows)
    if row is None or row.get(column) is None:
        return {}
    return {Weight.COUNT.value: as_float(row[column])}


def _score_of(row: Mapping[str, Any], column: str) -> dict[str, float]:
    """A message's importance as a weight — absent where nothing scored it."""
    if row.get(column) is None:
        return {}
    return {Weight.IMPORTANCE.value: as_float(row[column])}


def _rank_of(row: Mapping[str, Any] | None, column: str) -> dict[str, float]:
    """An address's centrality as a weight — absent where no rebuild ran."""
    if row is None or row.get(column) is None:
        return {}
    return {Weight.PAGERANK.value: as_float(row[column])}


def _sent_at_of(row: Mapping[str, Any]) -> dict[str, str]:
    """When a message was sent, as the store spelled it."""
    when = row.get("sent_at")
    return {} if when is None else {"sent_at": as_text(when)}


def _domain_of(row: Mapping[str, Any] | None, column: str) -> dict[str, str]:
    """An address's domain, which is what a tooltip has to say about one."""
    if row is None or row.get(column) is None:
        return {}
    return {"domain": as_text(row[column])}


def _listed(value: Any) -> list[Any]:
    """A projected list column as a list, whatever the driver handed back."""
    return list(value) if isinstance(value, list | tuple) else []


def _at(values: Sequence[Any], index: int) -> Any:
    """One entry of a parallel column, or ``None`` where the lists disagree."""
    return values[index] if index < len(values) else None


def _limit(value: int) -> int:
    """A row ceiling a statement can be bound to.

    ``LIMIT 0`` is legal Cypher that returns nothing, so a caller's stray zero
    would draw an empty archive instead of raising; and the top is closed
    because this is a public surface, the same argument
    :func:`~mailarc_analytics.queries.reports._limit` makes.
    """
    return min(max(1, value), MAX_GRAPH_ROWS)


def _depth(value: int) -> int:
    """A reply-chain depth the statement can actually deliver."""
    return min(max(1, value), MAX_DEPTH)
