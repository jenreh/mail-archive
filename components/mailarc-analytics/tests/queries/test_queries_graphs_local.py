"""The subgraph reader against a real graph, over the corpus that was planted.

Everything above this file works from scripted rows, which proves the reader
folds what it is handed and nothing about what a FalkorDB hands it. So the
planted corpus is archived with the real writer, rebuilt with the real rebuild,
and read back with the real reader — and what is asserted is the mail that was
*planted*: the five messages of the NORD-42 project, the two people from
``kunde.example`` who worked on it, the one reply the corpus contains and the
one circle the rebuild finds.

Four claims and each is a different half of the module. The topic view is the
composition working — two statements, one hub, no cross-multiplication. The ego
view is the two conversation reads agreeing: ``p1`` is reachable from ``p2``
through the header it answered *and* through the provider's thread, and one
node carries both. The path is the store's own procedure, whose rows are lists
this reader has to zip into edges. The overview is the aggregate edge, which
exists in no store and is counted through the mail.
"""

from functools import partial

import corpus
import planted_graph
import pytest
from runic.ogm import Session

from mailarc_analytics import rebuild_derived
from mailarc_analytics.queries.graphs import GraphReader
from mailarc_analytics.queries.model import NodeKind, Subgraph
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

PROJECT = ("p1", "p2", "p3", "p4", "p5")
"""The corpus's one topic — five messages joined by the ticket token."""

TAG_ID = "tag:nordlicht"
THREAD = f"{corpus.ACCOUNT_ID}:t-nord"


@pytest.fixture
def derived(archived: GraphConfig) -> GraphConfig:
    """The planted corpus with exactly one rebuild over it.

    One and not two: idempotence is ``test_derived_rebuild_local.py``'s claim,
    and the pictures below should fail for their own reasons.
    """
    with client.session(archived) as graph:
        rebuild_derived(graph, CONFIG)
    return archived


def _reader(config: GraphConfig) -> GraphReader:
    """The façade wired the way ``app/composition.py`` wires it."""
    return GraphReader(partial(client.session, config))


def _the_topic(config: GraphConfig) -> str:
    """The id of the corpus's only topic, read out of the graph.

    Asked for rather than computed: ``topic_id`` is a digest of the members and
    a test that recomputed it would agree with the rebuild by construction.
    R7 is the same fact from the other side — the id is not a durable reference,
    which is why nothing here writes one down.
    """
    with client.session(config) as graph:
        found = _ids_of(graph, "Topic")
    assert len(found) == 1, "the corpus plants exactly one topic"
    return found[0]


def _the_circle(config: GraphConfig) -> str:
    """The id of the corpus's only community."""
    with client.session(config) as graph:
        found = _ids_of(graph, "Community")
    assert len(found) == 1, "the corpus plants exactly one circle"
    return found[0]


def _ids_of(session: Session, label: str) -> list[str]:
    return [
        str(row[0])
        for row in session.execute(f"MATCH (n:{label}) RETURN n.id ORDER BY n.id").rows
    ]


def _ids(found: Subgraph) -> set[str]:
    return {one.id for one in found.nodes}


def _kinds(found: Subgraph, kind: NodeKind) -> set[str]:
    return {one.id for one in found.nodes if one.kind is kind}


def _edges(found: Subgraph) -> set[tuple[str, str, str]]:
    return {(one.source, one.target, one.kind) for one in found.edges}


def test_the_project_topic_holds_its_mail_and_the_people_on_it(
    derived: GraphConfig,
) -> None:
    """§6's first claim: p1-p5, Anna and Thomas, round one hub.

    Both people are in it although neither is on every message — Anna sent two
    of the five and Thomas one — because the participation edge is counted over
    the topic's mail rather than read off any one message.
    """
    topic = _the_topic(derived)

    found = _reader(derived).topic(topic)

    assert _kinds(found, NodeKind.MESSAGE) == {corpus.canonical(one) for one in PROJECT}
    assert {corpus.ANNA, corpus.THOMAS} <= _kinds(found, NodeKind.ADDRESS)
    assert _kinds(found, NodeKind.TOPIC) == {topic}
    assert not found.truncated


def test_every_member_hangs_off_the_topic_by_the_signal_that_found_it(
    derived: GraphConfig,
) -> None:
    """The edge carries the method, which is what tells a fact from a
    suggestion — and every one of these five was joined by the ticket token."""
    topic = _the_topic(derived)

    found = _reader(derived).topic(topic)

    about = {one.source: one.label for one in found.edges if one.kind == "ABOUT"}
    assert set(about) == {corpus.canonical(one) for one in PROJECT}
    assert set(about.values()) == {"ref"}


def test_the_ego_view_of_a_reply_reaches_its_parent_twice_over(
    derived: GraphConfig,
) -> None:
    """§6's second claim: ``p2`` finds ``p1`` through the reply *and* the thread.

    Two reads answer with the same message and the picture holds one node for
    it — a canvas refuses a duplicate id — carrying the ``REPLIES_TO`` edge from
    one and the ``IN_THREAD`` edge from the other. It is the only pair in the
    corpus that is both, which is what makes it the case worth planting.
    """
    found = _reader(derived).message(corpus.canonical("p2"))

    parent = corpus.canonical("p1")
    seed = corpus.canonical("p2")
    assert [one.id for one in found.nodes].count(parent) == 1
    assert (seed, parent, "REPLIES_TO") in _edges(found)
    assert {(seed, THREAD, "IN_THREAD"), (parent, THREAD, "IN_THREAD")} <= _edges(found)


def test_the_ego_view_carries_the_headers_and_the_findings(
    derived: GraphConfig,
) -> None:
    """The other four reads of the same view: who was on it, and what the
    rebuild filed it under."""
    found = _reader(derived).message(corpus.canonical("p2"))

    seed = corpus.canonical("p2")
    assert (seed, corpus.ANNA, "SENT_FROM") in _edges(found)
    assert (seed, corpus.OWN, "SENT_TO") in _edges(found)
    assert (seed, corpus.THOMAS, "COPIED_TO") in _edges(found)
    assert _kinds(found, NodeKind.TOPIC) == {_the_topic(derived)}
    assert _kinds(found, NodeKind.COMMUNITY) == {_the_circle(derived)}


def test_two_correspondents_are_one_hop_apart(derived: GraphConfig) -> None:
    """§6's third claim. Anna and Thomas are on ``p1`` together, so the archive
    holds the ``CO_ADDRESSED`` edge between them and the shortest route is it.

    The way round through the archive's owner is in the picture as well, and
    that is the procedure rather than the reader: ``algo.SPpaths`` asked for
    three paths answers with the three *shortest*, not with three copies of the
    shortest. Measured here rather than assumed, because it is what "show me
    the path" actually draws — the direct line, and the next-best answer to
    "how else are these two connected".
    """
    found = _reader(derived).path(corpus.ANNA, corpus.THOMAS)

    assert (corpus.ANNA, corpus.THOMAS, "CO_ADDRESSED") in _edges(found)
    assert _ids(found) == {corpus.ANNA, corpus.THOMAS, corpus.OWN}
    assert _edges(found) == {
        (corpus.ANNA, corpus.THOMAS, "CO_ADDRESSED"),
        (corpus.ANNA, corpus.OWN, "CO_ADDRESSED"),
        (corpus.OWN, corpus.THOMAS, "CO_ADDRESSED"),
    }


def test_a_pair_with_nothing_between_them_draws_nothing(
    derived: GraphConfig,
) -> None:
    """The Bcc'd address is on no visible line of any message, so A1 never
    pairs it — and a path search for it is an empty answer rather than an
    error."""
    found = _reader(derived).path(corpus.REVISION, corpus.THOMAS)

    assert found == Subgraph()


def test_the_overview_joins_a_topic_to_a_circle(derived: GraphConfig) -> None:
    """§6's fourth claim, and the one edge in this module that no store holds:
    the project's mail is also the circle's mail, and the map says so with a
    number instead of five lines through the messages."""
    topic, circle = _the_topic(derived), _the_circle(derived)

    found = _reader(derived).overview()

    assert {topic, circle} <= _ids(found)
    assert (topic, circle, "OVERLAPS") in _edges(found)
    overlap = next(one for one in found.edges if one.kind == "OVERLAPS")
    assert overlap.weight == len(PROJECT)


def test_a_tag_comes_back_with_the_mail_a_person_filed_under_it(
    derived: GraphConfig,
) -> None:
    """The annotation layer, written the only way it may be written and read
    back through the same reader — and the durable reference a topic id is
    not."""
    planted_graph.plant_tag(
        derived, TAG_ID, "Nordlicht", [corpus.canonical(one) for one in PROJECT[:2]]
    )

    found = _reader(derived).tag(TAG_ID)

    assert _kinds(found, NodeKind.TAG) == {TAG_ID}
    assert _kinds(found, NodeKind.MESSAGE) == {
        corpus.canonical(one) for one in PROJECT[:2]
    }
    assert {one.label for one in found.edges if one.kind == "TAGGED"} == {"manual"}


def test_an_address_view_holds_its_mail_and_its_neighbours(
    derived: GraphConfig,
) -> None:
    """One correspondent from both sides: the messages they are on, and the
    people they are written to together with."""
    found = _reader(derived).address(corpus.ANNA)

    assert _kinds(found, NodeKind.ADDRESS) == {corpus.ANNA, corpus.OWN, corpus.THOMAS}
    assert corpus.canonical("p1") in _kinds(found, NodeKind.MESSAGE)
    assert (corpus.ANNA, corpus.THOMAS, "CO_ADDRESSED") in _edges(found)


def test_a_circle_holds_the_people_the_partition_put_in_it(
    derived: GraphConfig,
) -> None:
    """Label propagation over ``CO_ADDRESSED`` finds one circle in the corpus,
    and it is the three addresses that are ever written to together."""
    found = _reader(derived).community(_the_circle(derived))

    assert _kinds(found, NodeKind.ADDRESS) == {
        corpus.ANNA,
        corpus.OWN,
        corpus.THOMAS,
    }
    assert corpus.canonical("p1") in _kinds(found, NodeKind.MESSAGE)


def test_a_view_that_hit_its_ceiling_says_what_it_left_out(
    derived: GraphConfig,
) -> None:
    """Against a real graph rather than a scripted one, because "the read came
    back full" is a property of what the store returned."""
    found = _reader(derived).topic(_the_topic(derived), limit=2)

    assert found.truncated
    assert found.notice == "Cut to the first 2: members, participants."
