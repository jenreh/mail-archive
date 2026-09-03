"""§5.1 against a real store — the probe, the guard, and what LPA actually does.

Three things only a running FalkorDB can settle, and every one of them was
measured here rather than read off documentation.

**The procedures throw.** ``algo.labelPropagation`` and ``algo.betweenness``
raise ``ResponseError: … configuration, unknown label Address`` on a graph that
has no ``Address`` node — which is exactly what an archive looks like before its
first import, and close enough to what it looks like before its first rebuild.
``algo.pageRank`` is the odd one out and answers with no rows instead. So the
guard cannot be "call it and see"; it is a probe plus a ``try`` around every
call, and what it stepped over is counted.

**The partition has to be planted.** The spec expects label propagation to
separate ``kunde.example`` from ``nordlicht.example`` in the planted corpus, and
it does not — measured. The corpus's whole co-addressing graph is *one*
triangle, ``anna``–``thomas``–``jens``, spanning both domains, plus five
addresses nobody was ever co-addressed with. That is a property of the corpus
rather than of the algorithm: block P puts all three people on every message.
So the separation is planted as two disjoint cliques, which is also what R1 asks
for — two *unambiguous* cliques are where a seedless algorithm has nothing to
be ambiguous about.

**R1 is answered at partition level.** Label propagation takes no seed, so this
file asserts that two runs put the same addresses together, never that they
chose the same community numbers. The numbers are gone by the time anything is
written: :func:`~mailarc_analytics.derived.model.community_id` keys a circle by
the digest of its members.
"""

from collections.abc import Mapping

import corpus
import planted_graph
import pytest
from runic.ogm import Session

from mailarc_analytics.derived.algorithms import (
    BETWEENNESS_PROCEDURE,
    LPA_PROCEDURE,
    PAGERANK_PROCEDURE,
    address_betweenness,
    graph_algorithms,
    label_propagation,
    message_pagerank,
)
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

LEFT = tuple(f"p{index}@links.example" for index in range(4))
RIGHT = tuple(f"q{index}@rechts.example" for index in range(4))
"""Two cliques with no edge between them — the unambiguous case R1 asks for."""


def _plant_two_cliques(session: Session) -> None:
    """Two complete co-addressing graphs, disjoint, and nothing else.

    Written as raw Cypher rather than as archived mail, the way
    ``test_derived_rebuild_local.py`` plants its stale edge: what is under test
    is a procedure reading ``(:Address)-[:CO_ADDRESSED]-(:Address)``, and
    producing that shape through the parser and the archiver would make the
    fixture the thing most likely to break.
    """
    for clique in (LEFT, RIGHT):
        for address in clique:
            session.execute("MERGE (a:Address {id: $id})", {"id": address})
        for one in clique:
            for other in clique:
                if one < other:
                    session.execute(
                        "MATCH (a:Address {id: $left}), (b:Address {id: $right}) "
                        "MERGE (a)-[r:CO_ADDRESSED]->(b) SET r.count = 3",
                        {"left": one, "right": other},
                    )


def _grouped(labels: Mapping[str, int]) -> set[frozenset[str]]:
    """The partition itself, with the algorithm's own numbering thrown away."""
    found: dict[int, set[str]] = {}
    for address, number in labels.items():
        found.setdefault(number, set()).add(address)
    return {frozenset(members) for members in found.values()}


def test_the_probe_names_every_procedure_this_phase_calls(
    config: GraphConfig,
) -> None:
    """``dbms.procedures`` is the one call that is safe on an empty graph.

    It names no label and no relationship type, so there is nothing for it to
    be unknown about — which is why the guard starts here and why this test
    needs no fixture beyond a graph name.
    """
    with client.session(config) as session:
        found = graph_algorithms(session)

    assert {LPA_PROCEDURE, PAGERANK_PROCEDURE, BETWEENNESS_PROCEDURE} <= found


def test_the_probe_answers_in_the_store_s_own_spelling_lower_cased(
    config: GraphConfig,
) -> None:
    """The binary writes ``algo.WCC`` and ``algo.BFS``; its errors lower-case
    them. One spelling has to win, and the lower-cased one is the one a caller
    can compare against a name it wrote itself."""
    with client.session(config) as session:
        found = graph_algorithms(session)

    assert found == {name.lower() for name in found}
    assert "algo.wcc" in found


def test_the_probe_is_asked_once_per_session(config: GraphConfig) -> None:
    """A round trip per procedure call would be one per stage, per rebuild.

    The same object comes back, which is what a cache keyed on the session
    means — and the cache is weak, so a session that goes away takes its entry
    with it rather than keeping a closed connection alive.
    """
    with client.session(config) as session:
        assert graph_algorithms(session) is graph_algorithms(session)


def test_label_propagation_separates_two_cliques(config: GraphConfig) -> None:
    """The finding, on the graph shape a circle actually has."""
    with client.session(config) as session:
        _plant_two_cliques(session)
        found = label_propagation(
            session, max_iterations=CONFIG.community_max_iterations
        )

    assert found.skipped == 0
    assert _grouped(found.labels) == {frozenset(LEFT), frozenset(RIGHT)}


def test_two_runs_put_the_same_people_together(config: GraphConfig) -> None:
    """R1, at the level the answer is actually used at.

    Not byte level: the community *numbers* are the algorithm's own and it has
    no seed, so asserting on them would be asserting that an unseeded
    procedure is deterministic. What has to hold is that the partition is the
    same, because that is what
    :func:`~mailarc_analytics.derived.model.community_id` hashes.
    """
    with client.session(config) as session:
        _plant_two_cliques(session)
        first = label_propagation(session, max_iterations=20)
        again = label_propagation(session, max_iterations=20)

    assert _grouped(first.labels) == _grouped(again.labels)


def test_the_direction_the_edge_was_stored_in_does_not_decide_the_partition(
    config: GraphConfig,
) -> None:
    """R2. ``CO_ADDRESSED`` is written smaller-id-first and read undirected.

    Both cliques are planted with every arrow pointing from the smaller id to
    the larger one, so a procedure that followed the arrow would drag both
    cliques towards their alphabetically-last member. Label propagation spreads
    a label along every incident edge, which is why it may read this edge where
    a PageRank may not.
    """
    with client.session(config) as session:
        _plant_two_cliques(session)
        found = label_propagation(session, max_iterations=20)

    assert len(_grouped(found.labels)) == 2


def test_the_planted_corpus_has_one_circle_and_it_spans_both_domains(
    archived: GraphConfig,
) -> None:
    """Measured, and recorded here because the spec expected the opposite.

    Block P puts ``jens``, ``anna`` and ``thomas`` on every one of its five
    messages, so the corpus's entire co-addressing graph is that triangle —
    ``kunde.example`` and ``nordlicht.example`` are one community and there is
    nothing in the corpus to separate. Everybody else was written to alone.
    """
    from mailarc_analytics import rebuild_derived

    with client.session(archived) as session:
        rebuild_derived(session, CONFIG)
        found = label_propagation(session, max_iterations=20)

    circles = {members for members in _grouped(found.labels) if len(members) > 1}
    assert circles == {frozenset({corpus.OWN, corpus.ANNA, corpus.THOMAS})}


def test_the_reply_pagerank_ranks_the_message_the_conversation_hangs_off(
    archived: GraphConfig,
) -> None:
    """The one PageRank that runs in the store, on the one directed edge.

    ``(reply)-[:REPLIES_TO]->(parent)`` means what an arrow is supposed to
    mean, so the message the corpus's only reply points at has to come out on
    top.
    """
    from mailarc_analytics import rebuild_derived

    with client.session(archived) as session:
        rebuild_derived(session, CONFIG)
        found = message_pagerank(session)

    assert found.skipped == 0
    assert max(found.scores, key=lambda one: found.scores[one]) == corpus.canonical(
        "p1"
    )


def test_on_an_empty_graph_the_procedures_that_throw_are_skipped_and_counted(
    config: GraphConfig,
) -> None:
    """R3. The state of an archive before its first import.

    ``unknown label Address`` is what the store answers, and a rebuild that let
    it out would fail on the one archive the guard exists for. Skipped, counted
    and the stage reports zero.
    """
    with client.session(config) as session:
        partition = label_propagation(session, max_iterations=20)
        bridges = address_betweenness(session, sampling_size=4, seed=7)

    assert (partition.labels, partition.skipped) == ({}, 1)
    assert (bridges.scores, bridges.skipped) == ({}, 1)


def test_the_pagerank_guard_is_a_formality_this_backend_does_not_need(
    config: GraphConfig,
) -> None:
    """Measured on FalkorDB 4.20.3, and recorded so nobody re-measures it.

    ``algo.pageRank`` answers with no rows on a graph that has neither the
    label nor the relationship type, where its siblings raise. The guard stays
    for symmetry — a backend that changed its mind here would be caught by the
    ``try`` rather than by a failed rebuild — but nothing is counted.
    """
    with client.session(config) as session:
        found = message_pagerank(session)

    assert (found.scores, found.skipped) == ({}, 0)


def test_a_sampling_size_of_zero_means_do_not_call_it_at_all(
    config: GraphConfig,
) -> None:
    """``betweenness_sampling`` is a size because the procedure refuses zero.

    "Do not run it" is the only way to spell off, and it is a *decision* rather
    than something the guard stepped over — so nothing is counted. A number in
    ``algorithms_skipped`` has to mean the store refused, or it stops being
    worth reading.
    """
    with client.session(config) as session:
        _plant_two_cliques(session)
        found = address_betweenness(session, sampling_size=0, seed=7)

    assert (found.scores, found.skipped) == ({}, 0)
    assert CONFIG.betweenness_sampling == 0


def test_betweenness_answers_when_it_is_switched_on(config: GraphConfig) -> None:
    """Off by default, and still has to work when a caller turns it on."""
    with client.session(config) as session:
        _plant_two_cliques(session)
        found = address_betweenness(session, sampling_size=4, seed=7)

    assert set(found.scores) == set(LEFT) | set(RIGHT)


def test_the_probe_is_what_stops_a_call_the_store_cannot_answer(
    config: GraphConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store without the procedure is skipped before a round trip.

    FalkorDB has all of them, so the missing-procedure case cannot be planted —
    the probe's answer is narrowed instead, which is the same input the guard
    would see on a backend that did not ship ``algo.labelPropagation``.
    """
    from mailarc_analytics.derived import algorithms

    monkeypatch.setattr(algorithms, "graph_algorithms", lambda session: frozenset())

    with client.session(config) as session:
        _plant_two_cliques(session)
        found = algorithms.label_propagation(session, max_iterations=20)

    assert (found.labels, found.skipped) == ({}, 1)


def test_the_corpus_is_archived_the_way_every_other_local_test_archives_it(
    archived: GraphConfig,
) -> None:
    """The fixture is the shared one, so this file measures the same graph the
    rest of the phase is measured against."""
    with client.session(archived) as session:
        counted = planted_graph.ground_truth(session)

    assert counted["Message"] == len(corpus.planted_corpus())
