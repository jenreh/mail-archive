"""Address centrality — why it is Python, and what it has to answer.

``CO_ADDRESSED`` is stored **directed, smaller id first**, which is an accident
of the writer and not a fact about anybody. A PageRank over that arrow would
rank an address by where its id sorts, and it would look exactly like
centrality while being a string comparison. So the number is a weighted,
undirected power iteration in Python over the pair counts the rebuild is
already holding, and the first test here is the one that would catch the
mistake: relabelling every address so the alphabet runs the other way must not
move the ranking.

Everything below is pure arithmetic over hand-built pairs. The planted corpus
has one three-person circle and no hub, which is the shape that cannot tell a
correct implementation from one that ranks by degree.
"""

import pytest

from mailarc_analytics import CoAddressedPair
from mailarc_analytics.derived.centrality import (
    DAMPING,
    ITERATIONS,
    weighted_pagerank,
)


def _pair(left: str, right: str, count: int = 1) -> CoAddressedPair:
    """One co-addressing pair. The value object orders the endpoints itself."""
    return CoAddressedPair(left=left, right=right, count=count)


def _star(spokes: int) -> tuple[CoAddressedPair, ...]:
    """A hub written to together with *spokes* people who never meet."""
    return tuple(_pair("hub", f"s{index:02d}") for index in range(spokes))


def test_the_hub_of_a_star_ranks_above_every_spoke() -> None:
    """The finding the number exists for, on the smallest graph that has one."""
    ranks = weighted_pagerank(_star(6))

    assert max(ranks, key=lambda one: ranks[one]) == "hub"
    assert all(ranks["hub"] > ranks[f"s{index:02d}"] for index in range(6))


def test_a_symmetric_graph_answers_symmetrically() -> None:
    """Two triangles joined by nothing rank identically, member for member.

    A rank that differed between the two would be a rank of the id, which is
    the whole reason this is not ``algo.pageRank`` over ``CO_ADDRESSED``.
    """
    pairs = (
        _pair("a1", "a2"), _pair("a2", "a3"), _pair("a1", "a3"),
        _pair("b1", "b2"), _pair("b2", "b3"), _pair("b1", "b3"),
    )  # fmt: skip

    ranks = weighted_pagerank(pairs)

    assert {round(ranks[one], 9) for one in ranks} == {round(ranks["a1"], 9)}


def test_the_arrow_the_store_keeps_does_not_decide_the_ranking() -> None:
    """R2, measured. The pair order is the writer's, never the analysis's.

    The same star with the hub's id sorted *after* every spoke instead of
    before it. ``CoAddressedPair`` puts the smaller id in ``left``, so the
    edges arrive pointing the other way round — and the hub still has to win.
    """
    ranks = weighted_pagerank(
        tuple(_pair("zzz-hub", f"s{index:02d}") for index in range(6))
    )

    assert max(ranks, key=lambda one: ranks[one]) == "zzz-hub"


def test_a_heavier_pair_pulls_more_than_a_light_one() -> None:
    """The count is a weight and not a placeholder.

    Two spokes at the same distance from the hub, one of them on twenty
    messages with it and the other on one. Ignoring ``count`` answers with two
    equal ranks, which is the failing case.
    """
    ranks = weighted_pagerank((_pair("hub", "often", 20), _pair("hub", "once", 1)))

    assert ranks["often"] > ranks["once"]


def test_two_runs_over_the_same_pairs_agree_to_the_bit() -> None:
    """Idempotence is the phase's contract and floats are where it is lost.

    The pairs are handed over in two different orders, because a rebuild's own
    order is a dict iteration away from changing and float addition is not
    associative. Summing in a sorted order is what makes the two equal rather
    than merely close.
    """
    pairs = (_pair("a", "b", 3), _pair("b", "c", 2), _pair("a", "c", 5))

    assert weighted_pagerank(pairs) == weighted_pagerank(tuple(reversed(pairs)))


def test_the_ranks_come_back_in_a_stable_order() -> None:
    """Sorted by id, so the rows a writer builds from it are stable too."""
    ranks = weighted_pagerank((_pair("m", "a"), _pair("z", "b")))

    assert list(ranks) == sorted(ranks)


def test_no_pairs_at_all_is_an_empty_answer_and_not_a_crash() -> None:
    """The state of every archive before its first rebuild."""
    assert weighted_pagerank(()) == {}


def test_the_edge_ceiling_keeps_the_first_pairs_and_says_what_it_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R4's ceiling: a bounded walk, and a number for what it cost.

    The pairs arrive in canonical order, so the prefix a ceiling keeps is
    reproducible — which the idempotence contract needs — and the addresses
    beyond it are simply not ranked. A silent cap would be indistinguishable
    from an archive where those people were never written to.
    """
    pairs = (_pair("a", "b"), _pair("c", "d"), _pair("e", "f"))

    with caplog.at_level("WARNING"):
        ranks = weighted_pagerank(pairs, max_edges=2)

    assert set(ranks) == {"a", "b", "c", "d"}
    assert "1" in caplog.text


def test_a_ceiling_of_zero_means_no_ceiling() -> None:
    """The same spelling ``max_messages`` uses, so one rule covers both."""
    pairs = (_pair("a", "b"), _pair("c", "d"))

    assert set(weighted_pagerank(pairs, max_edges=0)) == {"a", "b", "c", "d"}


def test_the_defaults_are_the_calibration_the_ceiling_was_measured_against() -> None:
    """R4 costs "two million pairs times twenty iterations"; pin the twenty.

    The damping factor is the one every PageRank uses, and neither is a
    setting: they only mean anything together with the ceiling, and exposing
    them would let an edit make the number incomparable between two archives
    without changing anything a user could see.
    """
    assert (DAMPING, ITERATIONS) == (0.85, 20)


def test_one_pair_splits_the_mass_evenly() -> None:
    """Two people written to together and nobody else are equally central.

    The smallest closed-form case, and the one that catches a power iteration
    that forgot to divide by the neighbour's own weight.
    """
    ranks = weighted_pagerank((_pair("a", "b"),))

    assert ranks == {"a": 0.5, "b": 0.5}


def test_an_address_co_addressed_with_itself_contributes_no_edge() -> None:
    """A1 cannot produce one — it walks pairs of a set — but a hand-built
    finding can, and a self-loop would give that address a share of its own
    rank on every pass."""
    ranks = weighted_pagerank((_pair("a", "a", 9), _pair("a", "b")))

    assert ranks == {"a": 0.5, "b": 0.5}
