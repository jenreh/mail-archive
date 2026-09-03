"""The subgraph reader's own decisions, with a scripted session for a graph.

Everything here is a property of :mod:`mailarc_analytics.queries.graphs` rather
than of a store: which statements a view asks and with what bound, what it does
with the rows — dedup, degree, normalisation, the depth cut, the truncation
notice — and what a caller may merge afterwards. A scripted session says all of
that in a line where a planted corpus says it in thirty, and it says one thing a
corpus cannot: that a view ran *this* statement and not another, because the
script is keyed by the catalogue constant itself.

The pattern is ``mailarc-ui``'s ``insights_archive.FakeGraph`` and deliberately
a copy of it rather than an import — a component may not reach into another
component's tests, and forty lines are cheaper than the coupling. It answers
through both members :func:`~mailarc_analytics.queries.rows.rows_of`
dispatches to: ``all_rows`` for the builder statements and ``execute`` for the
two that are raw Cypher.

Whether the statements return what these fakes pretend they return is
``test_queries_catalog_local.py``'s question, and whether the corpus really
holds the project and the circle they describe is
``test_queries_graphs_local.py``'s.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict
from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.graphs import (
    GRAPH_LIMIT,
    MAX_GRAPH_ROWS,
    GraphReader,
)
from mailarc_analytics.queries.model import (
    GraphEdge,
    GraphNode,
    NodeKind,
    Subgraph,
)

P1 = "p1@nordlicht.example"
P2 = "p2@nordlicht.example"
P3 = "p3@nordlicht.example"
ANNA = "anna.meier@kunde.example"
THOMAS = "thomas.blau@kunde.example"
OWN = "jens@nordlicht.example"
TOPIC = "topic:0000000000000000000000000000test"
CIRCLE = "community:0000000000000000000000000test"
TAG = "tag:nord"
THREAD = "1:t-nord"
WHEN = "2026-01-12T09:00:00+00:00"


class Reply(BaseModel):
    """One statement's answer as a driver gives it: a header and rows.

    Written column by column rather than as dicts because that is the shape the
    store really sends and the zip is what the session really does — the same
    reason ``test_queries_reports.py`` scripts its answers this way.
    """

    model_config = ConfigDict(frozen=True)

    columns: list[str] = []
    rows: list[list[Any]] = []


class Scripted:
    """A ``runic.ogm.Session`` stand-in that answers by statement.

    Keyed by the catalogue constant, so a test that plants rows for
    ``TOPIC_MEMBERS`` also proves the view ran that statement and not another.
    Doubles as its own session factory, and counts how often one was opened —
    "one session per view" is one of the things this module decides.
    """

    def __init__(self, answers: Mapping[Any, Reply] | None = None) -> None:
        self._answers = dict(answers or {})
        self.asked: list[tuple[Any, dict[str, Any]]] = []
        self.opened = 0

    def all_rows(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """How a builder statement is run: bound by the session, keyed by
        column."""
        return self._answer(statement, dict(params or {}))

    def execute(self, statement: str, params: Mapping[str, Any]) -> Any:
        """How the two raw statements are run: a header and a list of lists."""
        reply = self._answers.get(statement, Reply())
        self.asked.append((statement, dict(params)))
        return _Result(reply.columns, reply.rows)

    def _answer(self, statement: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.asked.append((statement, params))
        reply = self._answers.get(statement, Reply())
        return [dict(zip(reply.columns, row, strict=True)) for row in reply.rows]

    @contextmanager
    def open(self) -> Iterator[Session]:
        self.opened += 1
        yield cast(Session, self)

    @property
    def statements(self) -> list[Any]:
        return [statement for statement, _ in self.asked]

    def parameters(self, statement: Any) -> dict[str, Any]:
        """What the one call to *statement* bound."""
        return next(params for asked, params in self.asked if asked is statement)


class _Result:
    """What ``session.execute`` hands back for a raw statement."""

    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows


def _reader(answers: Mapping[Any, Reply] | None = None) -> tuple[Scripted, GraphReader]:
    fake = Scripted(answers)
    return fake, GraphReader(fake.open)


def _addresses(*rows: list[Any]) -> Reply:
    return Reply(
        columns=[
            "message_subject",
            "message_importance",
            "id",
            "domain",
            "rank",
            "kind",
        ],
        rows=list(rows),
    )


def _members(*rows: list[Any]) -> Reply:
    return Reply(
        columns=[
            "topic_label",
            "topic_messages",
            "id",
            "subject",
            "sent_at",
            "importance",
            "method",
        ],
        rows=list(rows),
    )


def _participants(*rows: list[Any]) -> Reply:
    return Reply(columns=["id", "domain", "rank", "messages"], rows=list(rows))


def _chain(*rows: list[Any]) -> Reply:
    return Reply(
        columns=["ids", "subjects", "importances", "sources", "targets"],
        rows=list(rows),
    )


def _node(found: Subgraph, node_id: str) -> GraphNode:
    return next(one for one in found.nodes if one.id == node_id)


def _ids(found: Subgraph) -> list[str]:
    return sorted(one.id for one in found.nodes)


def _edges(found: Subgraph) -> list[tuple[str, str, str]]:
    return sorted((one.source, one.target, one.kind) for one in found.edges)


class TestTheMessageView:
    """One message and everything the graph says about it."""

    def test_it_asks_its_six_statements_in_one_session(self) -> None:
        """Six round trips, one driver. A view is one question asked of one
        moment, and six connections for one canvas is five too many."""
        fake, reader = _reader()

        reader.message(P1)

        assert fake.opened == 1
        assert set(fake.statements) == {
            catalog.MESSAGE_ADDRESSES,
            catalog.THREAD_SIBLINGS,
            catalog.REPLY_CHAIN,
            catalog.MESSAGE_TOPICS,
            catalog.MESSAGE_TAGS,
            catalog.MESSAGE_CIRCLE,
        }
        assert fake.parameters(catalog.MESSAGE_ADDRESSES) == {"id": P1}

    def test_the_seed_wears_its_own_subject(self) -> None:
        """Which is why the message columns ride along on every address row:
        without them the node a user clicked would be labelled by its id."""
        fake, reader = _reader(
            {
                catalog.MESSAGE_ADDRESSES: _addresses(
                    ["Angebot", 0.5, OWN, "nordlicht.example", None, "SENT_FROM"],
                    ["Angebot", 0.5, ANNA, "kunde.example", None, "SENT_TO"],
                )
            }
        )

        found = reader.message(P1)

        seed = _node(found, P1)
        assert seed.kind is NodeKind.MESSAGE
        assert seed.label == "Angebot"
        assert _edges(found) == [(P1, ANNA, "SENT_TO"), (P1, OWN, "SENT_FROM")]

    def test_a_message_reached_twice_is_one_node(self) -> None:
        """A sibling that is also a reply comes back from two statements. The
        canvas gets one node with both of its edges, or cytoscape refuses the
        duplicate id outright."""
        fake, reader = _reader(
            {
                catalog.THREAD_SIBLINGS: Reply(
                    columns=[
                        "thread_id",
                        "thread_subject",
                        "id",
                        "subject",
                        "sent_at",
                        "importance",
                    ],
                    rows=[
                        [THREAD, "Angebot", P1, "Angebot", WHEN, None],
                        [THREAD, "Angebot", P2, "AW: Angebot", WHEN, None],
                    ],
                ),
                catalog.REPLY_CHAIN: _chain(
                    [[P2, P1], ["AW: Angebot", "Angebot"], [None, None], [P2], [P1]]
                ),
            }
        )

        found = reader.message(P2)

        assert _ids(found) == sorted([P1, P2, THREAD])
        assert _edges(found) == sorted(
            [
                (P1, THREAD, "IN_THREAD"),
                (P2, THREAD, "IN_THREAD"),
                (P2, P1, "REPLIES_TO"),
            ]
        )

    def test_the_depth_counts_hops_and_not_arrows(self) -> None:
        """The statement walks a fixed ``*1..3`` in both directions — a
        variable-length bound is not a parameter — so the depth a user sets is
        applied to the returned edges, and a reply *to* the seed is as near as
        a reply *by* it."""
        fake, reader = _reader(
            {
                catalog.REPLY_CHAIN: _chain(
                    [
                        [P2, P1, P3],
                        ["AW", "Angebot", "Rueckfrage"],
                        [None, None, None],
                        [P2, P3],
                        [P1, P2],
                    ]
                )
            }
        )

        near = reader.message(P2, depth=1)
        far = reader.message(P2, depth=2)

        assert _ids(near) == sorted([P1, P2, P3])
        assert _edges(near) == sorted([(P2, P1, "REPLIES_TO"), (P3, P2, "REPLIES_TO")])
        assert _ids(far) == sorted([P1, P2, P3])
        assert len(far.edges) == 2

    def test_a_reply_two_hops_out_is_left_out_at_depth_one(self) -> None:
        """The cut is a walk over the returned edges rather than a slice of
        them: a chain that reaches ``p3`` only through ``p1`` is a hop further
        out than one that reaches it directly."""
        fake, reader = _reader(
            {
                catalog.REPLY_CHAIN: _chain(
                    [
                        [P2, P1, P3],
                        ["AW", "Angebot", "Rueckfrage"],
                        [None, None, None],
                        [P2, P3],
                        [P1, P1],
                    ]
                )
            }
        )

        near = reader.message(P2, depth=1)
        far = reader.message(P2, depth=2)

        assert _ids(near) == sorted([P1, P2])
        assert _edges(near) == [(P2, P1, "REPLIES_TO")]
        assert _ids(far) == sorted([P1, P2, P3])

    def test_an_edge_two_paths_both_walk_is_drawn_once(self) -> None:
        """The chain read answers one row per path, so a busy thread hands the
        same first hop back over and over. A canvas draws one line for it."""
        fake, reader = _reader(
            {
                catalog.REPLY_CHAIN: _chain(
                    [[P2, P1], ["AW", "Angebot"], [None, None], [P2], [P1]],
                    [
                        [P2, P1, P3],
                        ["AW", "Angebot", "Rueckfrage"],
                        [None, None, None],
                        [P2, P3],
                        [P1, P1],
                    ],
                )
            }
        )

        found = reader.message(P2, depth=2)

        assert _edges(found) == sorted([(P2, P1, "REPLIES_TO"), (P3, P1, "REPLIES_TO")])

    def test_the_derived_neighbours_arrive_as_their_own_kinds(self) -> None:
        """A topic, a tag and a circle are three different things on a canvas,
        and the edge to each says which analysis put it there."""
        fake, reader = _reader(
            {
                catalog.MESSAGE_TOPICS: Reply(
                    columns=["id", "label", "message_count", "method"],
                    rows=[[TOPIC, "angebot", 5, "ref"]],
                ),
                catalog.MESSAGE_TAGS: Reply(
                    columns=["id", "name", "color"], rows=[[TAG, "Nord", "#123456"]]
                ),
                catalog.MESSAGE_CIRCLE: Reply(
                    columns=["id", "label", "size", "message_count", "score"],
                    rows=[[CIRCLE, "kunde.example", 3, 8, 0.75]],
                ),
            }
        )

        found = reader.message(P1)

        assert {one.id: one.kind for one in found.nodes} == {
            P1: NodeKind.MESSAGE,
            TOPIC: NodeKind.TOPIC,
            TAG: NodeKind.TAG,
            CIRCLE: NodeKind.COMMUNITY,
        }
        assert _edges(found) == sorted(
            [
                (P1, TOPIC, "ABOUT"),
                (P1, TAG, "TAGGED"),
                (P1, CIRCLE, "IN_CIRCLE"),
            ]
        )
        assert _node(found, TOPIC).label == "angebot"


class TestTheTopicView:
    """A piece of work, its mail and the people on it."""

    def test_it_asks_both_reads_with_the_topic_bound(self) -> None:
        fake, reader = _reader()

        reader.topic(TOPIC, limit=7)

        assert fake.statements == [catalog.TOPIC_MEMBERS, catalog.TOPIC_PARTICIPANTS]
        assert fake.parameters(catalog.TOPIC_MEMBERS) == {"topic": TOPIC, "limit": 7}

    def test_the_members_hang_off_the_topic_and_the_people_off_the_topic(
        self,
    ) -> None:
        """Two stars round one hub: the ``ABOUT`` edge the rebuild wrote, and
        the participation this reader counts. The second is an aggregate and
        says so — ``PARTICIPATES`` is not a type any store holds."""
        fake, reader = _reader(
            {
                catalog.TOPIC_MEMBERS: _members(
                    ["angebot", 5, P1, "Angebot", WHEN, 0.4, "ref"],
                    ["angebot", 5, P2, "AW: Angebot", WHEN, 0.8, "thread"],
                ),
                catalog.TOPIC_PARTICIPANTS: _participants([ANNA, "kunde", 0.5, 2]),
            }
        )

        found = reader.topic(TOPIC)

        assert _ids(found) == sorted([TOPIC, P1, P2, ANNA])
        assert _edges(found) == sorted(
            [
                (P1, TOPIC, "ABOUT"),
                (P2, TOPIC, "ABOUT"),
                (ANNA, TOPIC, "PARTICIPATES"),
            ]
        )
        assert _node(found, TOPIC).label == "angebot"

    def test_the_degree_is_counted_off_the_edges_and_normalised(self) -> None:
        """Nothing in the store knows a node's degree in a *subgraph*, which is
        the only degree a canvas can size by. The hub takes the whole of it."""
        fake, reader = _reader(
            {
                catalog.TOPIC_MEMBERS: _members(
                    ["angebot", 5, P1, "Angebot", WHEN, None, "ref"],
                    ["angebot", 5, P2, "AW: Angebot", WHEN, None, "ref"],
                ),
                catalog.TOPIC_PARTICIPANTS: _participants([ANNA, "kunde", None, 2]),
            }
        )

        found = reader.topic(TOPIC)

        assert _node(found, TOPIC).weights["degree"] == 1.0
        assert _node(found, P1).weights["degree"] == pytest.approx(1 / 3)
        assert _node(found, ANNA).weights["degree"] == pytest.approx(1 / 3)

    def test_every_weight_is_normalised_within_the_subgraph(self) -> None:
        """A canvas maps a weight onto a diameter, so the scale has to be the
        picture rather than the archive: the heaviest node in *this* view is
        1.0 and everything else is a fraction of it."""
        fake, reader = _reader(
            {
                catalog.TOPIC_MEMBERS: _members(
                    ["angebot", 5, P1, "Angebot", WHEN, 0.4, "ref"],
                    ["angebot", 5, P2, "AW: Angebot", WHEN, 0.8, "ref"],
                ),
                catalog.TOPIC_PARTICIPANTS: _participants([ANNA, "kunde", 0.25, 2]),
            }
        )

        found = reader.topic(TOPIC)

        assert _node(found, P1).weights["importance"] == pytest.approx(0.5)
        assert _node(found, P2).weights["importance"] == pytest.approx(1.0)
        assert _node(found, ANNA).weights["pagerank"] == pytest.approx(1.0)
        assert "importance" not in _node(found, ANNA).weights

    def test_a_topic_no_rebuild_counted_carries_no_count_at_all(self) -> None:
        """A weight that is absent and a weight of zero must not be drawn the
        same: an interrupted rebuild leaves a topic with a null count, and the
        smallest circle on the canvas would read as "this holds nothing"."""
        fake, reader = _reader(
            {
                catalog.TOPIC_MEMBERS: _members(
                    ["angebot", None, P1, "Angebot", WHEN, None, "ref"]
                )
            }
        )

        found = reader.topic(TOPIC)

        assert "count" not in _node(found, TOPIC).weights
        assert _node(found, TOPIC).weights == {"degree": 1.0}

    def test_a_topic_nothing_points_at_is_an_empty_answer(self) -> None:
        """R7: a topic id is a hash of its members and is minted afresh by every
        rebuild, so a bookmarked link goes stale. An empty subgraph is what the
        page turns into "this topic was recomputed — pick it again"."""
        fake, reader = _reader()

        found = reader.topic("topic:gone")

        assert found == Subgraph()


class TestTheAddressView:
    """One correspondent, their mail and who they are written to with."""

    def test_it_draws_the_mail_and_the_co_addressing(self) -> None:
        fake, reader = _reader(
            {
                catalog.ADDRESS_MESSAGES: Reply(
                    columns=[
                        "address_domain",
                        "address_rank",
                        "id",
                        "subject",
                        "sent_at",
                        "importance",
                        "kind",
                    ],
                    rows=[["kunde.example", 0.5, P1, "Angebot", WHEN, 0.4, "SENT_TO"]],
                ),
                catalog.ADDRESS_NEIGHBOURS: Reply(
                    columns=["id", "domain", "rank", "together"],
                    rows=[[THOMAS, "kunde.example", 0.25, 3]],
                ),
            }
        )

        found = reader.address(ANNA)

        assert _ids(found) == sorted([ANNA, THOMAS, P1])
        assert _edges(found) == sorted(
            [(P1, ANNA, "SENT_TO"), (ANNA, THOMAS, "CO_ADDRESSED")]
        )
        assert _node(found, ANNA).props["domain"] == "kunde.example"

    def test_the_stored_pair_count_travels_on_the_edge(self) -> None:
        """Raw, unlike a node weight: an edge's number is what the rebuild
        counted, and the kind is what says what it counts."""
        fake, reader = _reader(
            {
                catalog.ADDRESS_NEIGHBOURS: Reply(
                    columns=["id", "domain", "rank", "together"],
                    rows=[[THOMAS, "kunde.example", None, 3]],
                )
            }
        )

        found = reader.address(ANNA)

        assert found.edges == (
            GraphEdge(source=ANNA, target=THOMAS, kind="CO_ADDRESSED", weight=3.0),
        )


class TestTheTagAndCommunityViews:
    """The annotation layer and the circles, read the same way."""

    def test_a_tag_gathers_its_messages(self) -> None:
        fake, reader = _reader(
            {
                catalog.TAG_MEMBERS: Reply(
                    columns=[
                        "tag_name",
                        "id",
                        "subject",
                        "sent_at",
                        "importance",
                        "source",
                    ],
                    rows=[["Nord", P1, "Angebot", WHEN, 0.4, "manual"]],
                )
            }
        )

        found = reader.tag(TAG)

        assert fake.parameters(catalog.TAG_MEMBERS)["tag"] == TAG
        assert _node(found, TAG).kind is NodeKind.TAG
        assert _node(found, TAG).label == "Nord"
        assert _edges(found) == [(P1, TAG, "TAGGED")]

    def test_a_circle_holds_its_people_and_its_mail(self) -> None:
        fake, reader = _reader(
            {
                catalog.COMMUNITY_MEMBERS: Reply(
                    columns=["community_label", "id", "domain", "rank"],
                    rows=[["kunde.example", ANNA, "kunde.example", 0.5]],
                ),
                catalog.COMMUNITY_MESSAGES: Reply(
                    columns=["id", "subject", "sent_at", "importance", "score"],
                    rows=[[P1, "Angebot", WHEN, 0.4, 0.75]],
                ),
            }
        )

        found = reader.community(CIRCLE)

        assert fake.parameters(catalog.COMMUNITY_MEMBERS)["community"] == CIRCLE
        assert _node(found, CIRCLE).label == "kunde.example"
        assert _edges(found) == sorted(
            [(ANNA, CIRCLE, "MEMBER_OF"), (P1, CIRCLE, "IN_CIRCLE")]
        )


class TestTheOverview:
    """The whole archive at one remove: collections and where they overlap."""

    def test_it_lists_the_three_collections_and_joins_them(self) -> None:
        fake, reader = _reader(
            {
                catalog.OVERVIEW_TOPICS: Reply(
                    columns=["id", "label", "message_count"],
                    rows=[[TOPIC, "angebot", 5]],
                ),
                catalog.OVERVIEW_COMMUNITIES: Reply(
                    columns=["id", "label", "size", "message_count"],
                    rows=[[CIRCLE, "kunde.example", 3, 8]],
                ),
                catalog.OVERVIEW_TAGS: Reply(
                    columns=["id", "name", "messages"], rows=[[TAG, "Nord", 2]]
                ),
                catalog.OVERVIEW_TOPIC_CIRCLE: Reply(
                    columns=["topic_id", "community_id", "messages"],
                    rows=[[TOPIC, CIRCLE, 4]],
                ),
                catalog.OVERVIEW_TAG_TOPIC: Reply(
                    columns=["tag_id", "topic_id", "messages"], rows=[[TAG, TOPIC, 2]]
                ),
            }
        )

        found = reader.overview()

        assert _ids(found) == sorted([TOPIC, CIRCLE, TAG])
        assert _edges(found) == sorted(
            [(TOPIC, CIRCLE, "OVERLAPS"), (TAG, TOPIC, "OVERLAPS")]
        )
        assert _node(found, CIRCLE).weights["count"] == 1.0

    def test_an_overlap_with_a_collection_it_did_not_list_is_dropped(self) -> None:
        """Every listing is cut at a limit, so an overlap can name a topic the
        overview stopped short of. An edge with an end nobody drew is what makes
        cytoscape throw rather than render."""
        fake, reader = _reader(
            {
                catalog.OVERVIEW_TOPICS: Reply(
                    columns=["id", "label", "message_count"],
                    rows=[[TOPIC, "angebot", 5]],
                ),
                catalog.OVERVIEW_TOPIC_CIRCLE: Reply(
                    columns=["topic_id", "community_id", "messages"],
                    rows=[[TOPIC, CIRCLE, 4]],
                ),
            }
        )

        found = reader.overview()

        assert _ids(found) == [TOPIC]
        assert found.edges == ()


class TestThePath:
    """How two people are connected, as the store's own shortest paths."""

    def test_it_draws_a_node_per_stop_and_an_edge_per_step(self) -> None:
        fake, reader = _reader(
            {
                catalog.SHORTEST_PATHS: Reply(
                    columns=["ids", "types"],
                    rows=[[[ANNA, OWN, THOMAS], ["CO_ADDRESSED", "CO_ADDRESSED"]]],
                )
            }
        )

        found = reader.path(ANNA, THOMAS)

        assert [one.kind for one in found.nodes] == [NodeKind.ADDRESS] * 3
        assert _edges(found) == sorted(
            [
                (ANNA, OWN, "CO_ADDRESSED"),
                (OWN, THOMAS, "CO_ADDRESSED"),
            ]
        )

    def test_it_binds_both_ends_and_both_ceilings(self) -> None:
        """An unbounded walk over the densest edge in the archive is what a
        path search without a length and a count ceiling is."""
        fake, reader = _reader()

        reader.path(ANNA, THOMAS, max_len=2)

        assert fake.parameters(catalog.SHORTEST_PATHS) == {
            "left": ANNA,
            "right": THOMAS,
            "max_len": 2,
            "path_count": 3,
        }

    def test_two_people_with_nothing_between_them_are_an_empty_answer(self) -> None:
        fake, reader = _reader()

        assert reader.path(ANNA, THOMAS) == Subgraph()


class TestExpanding:
    """One hop out of a node the user double-clicked, merged by the caller."""

    @pytest.mark.parametrize(
        ("kind", "statement"),
        [
            (NodeKind.MESSAGE, catalog.MESSAGE_ADDRESSES),
            (NodeKind.TOPIC, catalog.TOPIC_MEMBERS),
            (NodeKind.ADDRESS, catalog.ADDRESS_MESSAGES),
            (NodeKind.TAG, catalog.TAG_MEMBERS),
            (NodeKind.COMMUNITY, catalog.COMMUNITY_MEMBERS),
        ],
    )
    def test_it_expands_a_node_of_every_kind_that_has_neighbours(
        self, kind: NodeKind, statement: Any
    ) -> None:
        fake, reader = _reader()

        reader.expand("some-id", kind)

        assert statement in fake.statements

    def test_a_thread_has_nothing_of_its_own_to_expand(self) -> None:
        """A thread is a hub the message view draws; there is no read that
        starts at one, so expanding it asks nothing rather than raising."""
        fake, reader = _reader()

        found = reader.expand(THREAD, NodeKind.THREAD)

        assert found == Subgraph()
        assert fake.statements == []

    def test_a_merge_keeps_one_node_and_one_edge_per_pair(self) -> None:
        """What a canvas does with an expansion: the new subgraph is laid over
        the one on screen, and anything both of them hold is one thing."""
        left = Subgraph(
            nodes=(
                GraphNode(id=P1, kind=NodeKind.MESSAGE, label="Angebot"),
                GraphNode(id=TOPIC, kind=NodeKind.TOPIC, weights={"count": 1.0}),
            ),
            edges=(GraphEdge(source=P1, target=TOPIC, kind="ABOUT"),),
        )
        right = Subgraph(
            nodes=(
                GraphNode(id=P1, kind=NodeKind.MESSAGE, weights={"importance": 1.0}),
                GraphNode(id=P2, kind=NodeKind.MESSAGE, label="AW: Angebot"),
            ),
            edges=(
                GraphEdge(source=P1, target=TOPIC, kind="ABOUT"),
                GraphEdge(source=P2, target=TOPIC, kind="ABOUT"),
            ),
        )

        merged = left.merged_with(right)

        assert _ids(merged) == sorted([P1, P2, TOPIC])
        assert _edges(merged) == sorted([(P1, TOPIC, "ABOUT"), (P2, TOPIC, "ABOUT")])
        assert _node(merged, P1).label == "Angebot"
        assert _node(merged, P1).weights == {"importance": 1.0}

    def test_a_merge_says_what_either_half_left_out(self) -> None:
        left = Subgraph(nodes=(GraphNode(id=P1, kind=NodeKind.MESSAGE),))
        right = Subgraph(truncated=True, notice="Cut to the first 2: members.")

        merged = left.merged_with(right)

        assert merged.truncated
        assert merged.notice == "Cut to the first 2: members."


class TestWhatWasLeftOut:
    """A view that hit its ceiling has to say so, and say what was cut."""

    def test_a_read_that_filled_its_limit_marks_the_view_truncated(self) -> None:
        fake, reader = _reader(
            {
                catalog.TOPIC_MEMBERS: _members(
                    ["angebot", 5, P1, "Angebot", WHEN, None, "ref"],
                    ["angebot", 5, P2, "AW: Angebot", WHEN, None, "ref"],
                )
            }
        )

        found = reader.topic(TOPIC, limit=2)

        assert found.truncated
        assert found.notice == "Cut to the first 2: members."

    def test_a_view_that_fitted_says_nothing(self) -> None:
        fake, reader = _reader(
            {
                catalog.TOPIC_MEMBERS: _members(
                    ["angebot", 5, P1, "Angebot", WHEN, None, "ref"]
                )
            }
        )

        found = reader.topic(TOPIC, limit=2)

        assert not found.truncated
        assert found.notice == ""

    def test_the_notice_names_every_read_that_was_cut(self) -> None:
        fake, reader = _reader(
            {
                catalog.TOPIC_MEMBERS: _members(
                    ["angebot", 5, P1, "Angebot", WHEN, None, "ref"]
                ),
                catalog.TOPIC_PARTICIPANTS: _participants(
                    [ANNA, "kunde", None, 2], [THOMAS, "kunde", None, 1]
                ),
            }
        )

        found = reader.topic(TOPIC, limit=1)

        assert found.notice == "Cut to the first 1: members, participants."


class TestTheRowCeiling:
    """What a caller may ask for, and what it is clamped to."""

    @pytest.mark.parametrize(
        ("asked", "bound"),
        [(0, 1), (-5, 1), (7, 7), (MAX_GRAPH_ROWS + 1000, MAX_GRAPH_ROWS)],
    )
    def test_a_limit_is_clamped_at_both_ends(self, asked: int, bound: int) -> None:
        """``LIMIT 0`` is legal Cypher that returns nothing, so a stray zero
        would render as an archive with no mail in it; and this is a public
        surface, so the top has to be closed as well."""
        fake, reader = _reader()

        reader.topic(TOPIC, limit=asked)

        assert fake.parameters(catalog.TOPIC_MEMBERS)["limit"] == bound

    def test_the_default_is_a_canvas_worth_of_nodes(self) -> None:
        fake, reader = _reader()

        reader.topic(TOPIC)

        assert fake.parameters(catalog.TOPIC_MEMBERS)["limit"] == GRAPH_LIMIT

    @pytest.mark.parametrize(("asked", "nodes"), [(0, 2), (9, 3)])
    def test_the_depth_is_clamped_to_what_the_statement_walks(
        self, asked: int, nodes: int
    ) -> None:
        """The reply chain is a fixed ``*1..3`` in the store, so a depth of nine
        is a promise this reader cannot keep."""
        fake, reader = _reader(
            {
                catalog.REPLY_CHAIN: _chain(
                    [
                        [P2, P1, P3],
                        ["AW", "Angebot", "Rueckfrage"],
                        [None, None, None],
                        [P2, P3],
                        [P1, P1],
                    ]
                )
            }
        )

        found = reader.message(P2, depth=asked)

        assert len(found.nodes) == nodes
