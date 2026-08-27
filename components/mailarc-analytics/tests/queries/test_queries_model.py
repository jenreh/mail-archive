"""What "the edge and the ground truth agree" is allowed to mean.

The cross-check is the one thing on the insights page that is an *oracle*
rather than a view: it runs A1's definition and A1's materialisation against
each other and says which one is wrong. That only works if it is right about
both directions — a disagreement it misses is a bug shipped, and a
disagreement it invents is worse, because the first false alarm on a healthy
archive is the last one anybody reads.

Both statements are ``ORDER BY together DESC LIMIT $limit``, so on any real
archive both sides are top-N listings with an arbitrary tie-break at the
bottom. Most of this file is therefore about truncation: which pairs a cut
listing still proves something about, which it does not, and that the second
kind are counted rather than quietly dropped.

No graph anywhere — :meth:`CoAddressedAgreement.between` is a pure function
over two sequences, which is exactly why it can be asked these questions at
all. Whether the two listings agree *on a real archive* is
``test_queries_reports_local.py``'s question.
"""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from mailarc_analytics.derived.model import TemplateDirection
from mailarc_analytics.queries.model import (
    ArchivedDay,
    ArchiveTotals,
    CoAddressedAgreement,
    CoAddressedRow,
    ComparedPair,
    CoRecipientRow,
    GroupRow,
    TemplateRow,
    TopicRow,
)

WHEN = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

VALUE_OBJECTS: tuple[type[BaseModel], ...] = (
    ArchivedDay,
    ArchiveTotals,
    CoAddressedAgreement,
    CoAddressedRow,
    CoRecipientRow,
    ComparedPair,
    GroupRow,
    TemplateRow,
    TopicRow,
)
"""Every projection this package answers with, for the house rule below."""


def _truth(*pairs: tuple[str, str, int]) -> list[CoRecipientRow]:
    """A ``CO_RECIPIENTS`` listing, in the order the statement returns it."""
    return [
        CoRecipientRow(left_id=left, right_id=right, together=together)
        for left, right, together in pairs
    ]


def _edge(*pairs: tuple[str, str, int]) -> list[CoAddressedRow]:
    """A ``TOP_CO_ADDRESSED`` listing, spans filled in so the rows are real."""
    return [
        CoAddressedRow(
            left_id=left,
            right_id=right,
            together=together,
            first_seen=WHEN,
            last_seen=WHEN,
        )
        for left, right, together in pairs
    ]


def _pairs(found: tuple[ComparedPair, ...]) -> list[tuple[str, str]]:
    return [(one.left_id, one.right_id) for one in found]


class TestWhenNeitherSideWasCut:
    """Both listings came back short of the limit, so both are exhaustive."""

    def test_two_identical_listings_agree(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 5), ("anna", "team", 2)),
            _edge(("anna", "thomas", 5), ("anna", "team", 2)),
            limit=50,
        )

        assert found.agrees
        assert _pairs(found.matched) == [("anna", "thomas"), ("anna", "team")]
        assert found.compared == 2
        assert found.unjudged == 0

    def test_the_heaviest_pair_is_reported_first(self) -> None:
        """The statements rank by the count; a listing that re-sorted by
        address would bury the row a reader came for."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "team", 2), ("anna", "thomas", 5)),
            _edge(("anna", "team", 2), ("anna", "thomas", 5)),
            limit=50,
        )

        assert _pairs(found.matched) == [("anna", "thomas"), ("anna", "team")]

    def test_a_count_the_two_sides_disagree_on_is_reported(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 5)), _edge(("anna", "thomas", 4)), limit=50
        )

        assert not found.agrees
        assert found.count_mismatches == (
            ComparedPair(left_id="anna", right_id="thomas", truth=5, edge=4),
        )

    def test_a_pair_only_the_ground_truth_has_is_reported(self) -> None:
        """The edge listing is exhaustive here, so its silence really does mean
        the pair has no edge — a rebuild that has not run, or one that dropped
        the message for being too widely addressed."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 5), ("anna", "team", 2)),
            _edge(("anna", "thomas", 5)),
            limit=50,
        )

        assert not found.agrees
        assert found.truth_only == (
            ComparedPair(left_id="anna", right_id="team", truth=2, edge=None),
        )

    def test_a_pair_only_the_edge_has_is_reported(self) -> None:
        """The direction with no innocent explanation: no message puts these
        two on the same mail, and the edge says otherwise."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 5)),
            _edge(("anna", "thomas", 5), ("anna", "revision", 3)),
            limit=50,
        )

        assert not found.agrees
        assert found.edge_only == (
            ComparedPair(left_id="anna", right_id="revision", truth=None, edge=3),
        )

    def test_an_empty_archive_agrees_with_an_empty_derived_layer(self) -> None:
        found = CoAddressedAgreement.between([], [], limit=50)

        assert found.agrees
        assert found.compared == 0

    def test_a_derived_layer_nobody_has_built_disagrees_with_everything(
        self,
    ) -> None:
        """Not a false alarm: an archive with pairs and no edges really does
        differ from its ground truth, and "no rebuild has run yet" is the
        answer a reader needs to see rather than a clean bill of health."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 5)), [], limit=50
        )

        assert not found.agrees
        assert _pairs(found.truth_only) == [("anna", "thomas")]


class TestAPairCarryingTwoEdges:
    """The write-path bug this comparison is the only place that could see.

    ``MERGE_CO_ADDRESSED``'s own docstring names silently-doubled derived rows
    as the hazard the phase guards against. For edges, this is the guard: a
    pair with two physical ``CO_ADDRESSED`` relationships comes back as two
    rows keyed the same, and a dict comprehension quietly keeps whichever
    arrived last — so the check judged one arbitrary count and never saw the
    other, while both rows ate a slot against *limit*.
    """

    def test_two_rows_for_one_pair_are_counted_not_collapsed(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("a@x", "b@x", 2)),
            _edge(("a@x", "b@x", 2), ("a@x", "b@x", 99)),
            limit=500,
        )

        assert found.duplicate_pairs == 1

    def test_a_duplicated_pair_is_a_disagreement_however_it_is_counted(
        self,
    ) -> None:
        """Whichever of the two counts a dict happened to keep, the graph is
        wrong — so the verdict must not be allowed to come out clean."""
        found = CoAddressedAgreement.between(
            _truth(("a@x", "b@x", 2)),
            _edge(("a@x", "b@x", 2), ("a@x", "b@x", 2)),
            limit=500,
        )

        assert found.duplicate_pairs == 1
        assert found.agrees is False

    def test_one_row_per_pair_is_not_a_duplicate(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("a@x", "b@x", 2)), _edge(("a@x", "b@x", 2)), limit=500
        )

        assert found.duplicate_pairs == 0
        assert found.agrees is True


class TestTheOneUncheckedPrecondition:
    """``between`` is public and its soundness rests on one argument.

    Every floor it computes means "this listing was cut at *limit*", and the
    only thing that makes that true is the caller having fetched it with the
    same number. ``AnalyticsReader`` is internally consistent; a model driving
    phase 6's MCP server through the same exported constants need not be.
    """

    def test_more_rows_than_the_limit_is_refused_rather_than_judged(self) -> None:
        """Loud, because the alternative is silent: a limit larger than the
        fetch collapses both floors to zero and turns every asymmetry between
        two truncated listings into a reported disagreement."""
        with pytest.raises(ValueError, match="were actually fetched with"):
            CoAddressedAgreement.between(
                _truth(("a@x", "z@x", 5), ("b@x", "z@x", 4)), _edge(), limit=1
            )

    def test_exactly_the_limit_is_the_full_listing_and_is_fine(self) -> None:
        """The boundary the check must not move: ``len == limit`` is precisely
        what "this listing was cut" means."""
        found = CoAddressedAgreement.between(
            _truth(("a@x", "z@x", 5)), _edge(("a@x", "z@x", 5)), limit=1
        )

        assert found.truth_floor == 5
        assert found.agrees is True


class TestWhenBothSidesWereCut:
    """The wolf test. Two full listings, tie-broken differently at the bottom."""

    def test_the_floor_is_the_smallest_count_and_not_the_last_row(self) -> None:
        """``_floor`` reads ``min()`` off the whole listing on purpose.

        Its docstring says why — the rule stays true of any listing somebody
        hands in, not only of one this catalogue produced — and every listing
        the tests supply is already sorted, so ``counts[-1]`` agreed with
        ``min()`` on all of them and the decision was pinned by nothing.
        ``between`` is a public classmethod taking plain sequences; a listing
        out of order would otherwise compute a floor above some of its own rows
        and start reporting unjudged pairs as disagreements.
        """
        found = CoAddressedAgreement.between(
            _truth(("a@x", "z@x", 2), ("b@x", "z@x", 9), ("c@x", "z@x", 5)),
            _edge(),
            limit=3,
        )

        assert found.truth_floor == 2

    def test_pairs_below_both_cuts_are_not_ruled_on(self) -> None:
        """Both listings hold three rows out of an archive with more, and the
        third row differs because the store broke a tie one way here and the
        other way there. That is not a disagreement about the data, and a
        symmetric difference would report it as one every single time."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9), ("anna", "team", 5), ("bo", "cy", 2)),
            _edge(("anna", "thomas", 9), ("anna", "team", 5), ("dee", "eve", 2)),
            limit=3,
        )

        assert found.agrees
        assert _pairs(found.matched) == [("anna", "thomas"), ("anna", "team")]
        assert found.unjudged == 2

    def test_the_floor_is_where_each_listing_stopped(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9), ("anna", "team", 5), ("bo", "cy", 2)),
            _edge(("anna", "thomas", 9), ("anna", "team", 4), ("dee", "eve", 3)),
            limit=3,
        )

        assert (found.truth_floor, found.edge_floor) == (2, 3)

    def test_a_disagreement_above_the_cut_is_still_reported(self) -> None:
        """Truncation is not an excuse for anything the listings actually
        name — both counts were read, so the comparison is direct."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9), ("anna", "team", 5), ("bo", "cy", 2)),
            _edge(("anna", "thomas", 9), ("anna", "team", 4), ("dee", "eve", 2)),
            limit=3,
        )

        assert not found.agrees
        assert found.count_mismatches == (
            ComparedPair(left_id="anna", right_id="team", truth=5, edge=4),
        )

    def test_a_heavy_pair_the_edge_never_listed_is_reported(self) -> None:
        """Five is above the edge's floor of two, so the edge would have had to
        list it. Its absence is arithmetic, not truncation."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9), ("anna", "team", 5), ("bo", "cy", 2)),
            _edge(("anna", "thomas", 9), ("dee", "eve", 3), ("fay", "gus", 2)),
            limit=3,
        )

        assert _pairs(found.truth_only) == [("anna", "team")]
        assert _pairs(found.edge_only) == [("dee", "eve")]

    def test_a_pair_exactly_at_the_other_sides_floor_is_not_ruled_on(self) -> None:
        """The boundary is strict. A pair counted three by one side, with the
        other side cut at three, could sit just under the cut and be perfectly
        right — so nothing is claimed about it."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9), ("anna", "team", 4), ("bo", "cy", 3)),
            _edge(("anna", "thomas", 9), ("anna", "team", 4), ("dee", "eve", 3)),
            limit=3,
        )

        assert found.agrees
        assert found.unjudged == 2


class TestWhenOnlyOneSideWasCut:
    """The reason there are two floors and not one shared, conservative one."""

    def test_an_exhaustive_edge_settles_a_pair_the_cut_truth_still_names(
        self,
    ) -> None:
        """The edge listing came back short, so it holds every pair there is.
        Folding both sides into one floor would have left this unjudged and
        under-reported a real disagreement."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9), ("anna", "team", 5), ("bo", "cy", 2)),
            _edge(("anna", "thomas", 9), ("anna", "team", 5)),
            limit=3,
        )

        assert (found.truth_floor, found.edge_floor) == (2, 0)
        assert _pairs(found.truth_only) == [("bo", "cy")]
        assert found.unjudged == 0

    def test_an_exhaustive_truth_settles_a_pair_only_the_cut_edge_names(
        self,
    ) -> None:
        """Mirror image, and the loud one: no message in the archive puts these
        two together, whatever the edge says."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9), ("anna", "team", 5)),
            _edge(("anna", "thomas", 9), ("anna", "team", 5), ("dee", "eve", 2)),
            limit=3,
        )

        assert (found.truth_floor, found.edge_floor) == (0, 2)
        assert _pairs(found.edge_only) == [("dee", "eve")]


class TestWhichDisagreementsHaveNoInnocentReading:
    """Direction is the whole diagnosis; the buckets alone are not."""

    def test_an_edge_counting_higher_than_the_archive_is_called_out(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 5)), _edge(("anna", "thomas", 9)), limit=50
        )

        assert _pairs(found.edge_overstates) == [("anna", "thomas")]

    def test_an_edge_counting_lower_is_not(self) -> None:
        """A stale rebuild, a capped one and a mail to a distribution list all
        produce exactly this, so calling it a bug would cry wolf on nearly
        every real archive."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9)), _edge(("anna", "thomas", 5)), limit=50
        )

        assert not found.agrees
        assert found.edge_overstates == ()

    def test_a_pair_the_archive_has_never_seen_is_called_out(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 9)),
            _edge(("anna", "thomas", 9), ("anna", "revision", 3)),
            limit=50,
        )

        assert _pairs(found.edge_overstates) == [("anna", "revision")]

    def test_the_call_outs_are_ranked_by_the_loudest_claim(self) -> None:
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 2)),
            _edge(("anna", "thomas", 4), ("anna", "revision", 9)),
            limit=50,
        )

        assert _pairs(found.edge_overstates) == [
            ("anna", "revision"),
            ("anna", "thomas"),
        ]


class TestTheShapeOfAPair:
    """A pair is unordered in meaning and ordered in storage."""

    def test_the_two_sides_are_matched_whichever_order_they_arrive_in(self) -> None:
        """Both statements emit ``a.id < b.id`` today, so this changes nothing
        now. It is the difference between reporting one count mismatch and
        reporting a graph full of pairs each side supposedly has alone, the day
        a statement is edited and drops that filter."""
        found = CoAddressedAgreement.between(
            _truth(("anna", "thomas", 5)), _edge(("thomas", "anna", 4)), limit=50
        )

        assert found.count_mismatches == (
            ComparedPair(left_id="anna", right_id="thomas", truth=5, edge=4),
        )

    def test_a_side_that_said_nothing_weighs_nothing(self) -> None:
        assert ComparedPair(left_id="a", right_id="b", truth=7).heaviest == 7
        assert ComparedPair(left_id="a", right_id="b", edge=7).heaviest == 7

    @pytest.mark.parametrize("model", VALUE_OBJECTS)
    def test_every_row_is_a_frozen_pydantic_model(self, model: type[BaseModel]) -> None:
        """Frozen because a page holds one after the session that read it is
        gone: a mutable row is a row a template can edit, and the archive's
        answer would then be whatever was rendered last."""
        assert issubclass(model, BaseModel)
        assert model.model_config.get("frozen") is True

    def test_a_row_refuses_to_be_edited(self) -> None:
        row = CoRecipientRow(left_id="anna", right_id="thomas", together=5)

        with pytest.raises(ValidationError):
            row.together = 6  # ty: ignore[invalid-assignment]


class TestTheRowsThemselves:
    """The projections a page renders, and the defaults a sparse graph needs."""

    def test_a_group_row_survives_a_node_with_nothing_but_a_key(self) -> None:
        assert GroupRow(id="circle").message_count == 0

    def test_a_topic_row_keeps_the_method_that_drew_its_edges(self) -> None:
        """§6.2's whole point: ``ref`` is a fact and ``participants`` is a
        suggestion, and a reader has to be able to tell without clicking."""
        row = TopicRow(id="topic:1", label="angebot", method="ref", messages=5)

        assert (row.method, row.messages) == ("ref", 5)

    def test_a_template_row_carries_the_direction_it_was_asked_for(self) -> None:
        """The statement filters on it and therefore never returns it, and sent
        and received end up next to each other on the page — a row that lost
        its direction on the way is one nobody can rank honestly."""
        row = TemplateRow(id="template:1e16:sent", direction=TemplateDirection.SENT)

        assert row.direction is TemplateDirection.SENT

    def test_an_archived_day_defaults_to_a_day_on_which_nothing_happened(
        self,
    ) -> None:
        """The shape gap-filling needs: every field but the key has a zero, so
        a day the statement never returned is one constructor call rather than
        a second row type."""
        row = ArchivedDay(day="2026-03-01")

        assert (row.messages, row.bytes) == (0, 0)

    def test_totals_say_whether_a_rebuild_has_ever_run(self) -> None:
        """Zero derived over a non-empty archive is a different sentence from
        "the analyses found nothing", and a reader should not have to guess."""
        assert ArchiveTotals(messages=33).derived == 0
        assert ArchiveTotals(messages=33, topics=1, co_addressed=3).derived == 4
