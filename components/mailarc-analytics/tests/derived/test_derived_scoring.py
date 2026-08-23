"""``automation_score`` — three factors, any one of which may veto.

Frequency times regularity of the intervals times brevity, exactly as §6.3
asks. Multiplied and not averaged, so a text written twenty times at random
moments scores zero rather than two thirds, and a grunt sent every morning
scores nothing at all. Each factor is checked on its own here, because the
product hides which one moved.

The brevity floor gets a test of its own and the numbers in it are the whole
argument for it. Without the floor, brevity rises towards one as the body
shrinks and the best automation candidate in any archive is the pile of
one-word replies: measured, twenty-eight daily ``Danke!`` mails score 0.995
against the monthly status report's 0.64. With it they score 0.040 and rank
last, which is where they belong.
"""

from datetime import UTC, datetime, timedelta

import corpus
import pytest

from mailarc_analytics import automation_score

CONFIG = corpus.calibrated_config()
START = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _every(days: int, count: int) -> list[datetime | None]:
    """*count* sendings, exactly *days* apart — a perfect schedule."""
    return [START + timedelta(days=days * step) for step in range(count)]


class TestTheThreeFactors:
    """Each in ``0..1``, and each able to bring the product to zero."""

    def test_a_monthly_report_of_normal_length_scores_well(self) -> None:
        """Twelve sendings, evenly spaced, eighty-one words — the shape §6.3
        says a template ought to have.

        The literal has the ranking it exists to protect standing beside it. A
        bare golden invites the wrong repair: a legitimate recalibration of
        ``frequency_saturation`` or the two word counts moves the number, says
        nothing about whether the monthly report still wins, and the natural
        fix is to paste the new value in — at which point the assertion has
        stopped testing anything.
        """
        monthly = automation_score(_every(30, 12), 81, CONFIG)

        assert monthly == 0.711744
        assert monthly > automation_score(
            [START + timedelta(days=step) for step in (0, 1, 2, 90, 200, 201)],
            81,
            CONFIG,
        )
        assert monthly > automation_score(_every(30, 12), 600, CONFIG)

    def test_frequency_saturates_rather_than_growing_without_bound(self) -> None:
        """Two hundred newsletters must not out-rank twelve status reports.

        An unbounded frequency term would hand the ranking to the mailing list,
        which is the comparison the spec makes in as many words.
        """
        monthly = automation_score(_every(30, 12), 81, CONFIG)
        weekly = automation_score(_every(30, 200), 81, CONFIG)

        assert monthly == weekly

    def test_irregular_intervals_cost_more_than_a_few_missing_sendings(
        self,
    ) -> None:
        """Regularity is what separates a schedule from a habit."""
        even = automation_score(_every(30, 6), 81, CONFIG)
        uneven = automation_score(
            [START + timedelta(days=step) for step in (0, 1, 2, 90, 200, 201)],
            81,
            CONFIG,
        )

        assert uneven < even / 2

    def test_a_long_letter_scores_below_a_short_form(self) -> None:
        """Brevity is the factor that says "you could have templated this"."""
        form = automation_score(_every(30, 12), 60, CONFIG)
        letter = automation_score(_every(30, 12), 600, CONFIG)

        assert letter < form / 3


class TestTheBrevityFloor:
    """Why ``template_min_words`` exists, in the two numbers that justify it."""

    def test_a_one_word_reply_sent_daily_ranks_last(self) -> None:
        """Twenty-eight ``Danke!`` mails: frequent, perfectly regular, useless."""
        assert automation_score(_every(1, 28), 1, CONFIG) == 0.039801

    def test_without_the_floor_it_would_rank_first(self) -> None:
        """The same mails at ``template_min_words = 1`` beat everything.

        Nought point nine nine five against a monthly status report's nought
        point seven one — the floor is not a refinement, it is the difference
        between a useful list and a list of grunts.
        """
        without = CONFIG.model_copy(update={"template_min_words": 1})

        assert automation_score(_every(1, 28), 1, without) == 0.995025
        assert automation_score(_every(1, 28), 1, without) > automation_score(
            _every(30, 12), 81, CONFIG
        )


class TestTheDegenerateCases:
    """All four reachable ones, and the one that is not."""

    def test_everything_sent_in_the_same_second_scores_zero(self) -> None:
        """A mass mailing is the opposite of the schedule this looks for."""
        assert automation_score([START] * 5, 80, CONFIG) == 0.0

    def test_two_dated_members_score_zero(self) -> None:
        """One gap has no variation to measure, and calling that a perfect
        schedule would score a coincidence as a routine."""
        assert automation_score(_every(30, 2), 80, CONFIG) == 0.0

    def test_the_smallest_group_that_can_be_a_template_still_scores(self) -> None:
        """Three occurrences is ``template_min_occurrences``; it must not be a
        degenerate case, or the threshold would exclude what it admits."""
        assert automation_score(_every(30, 3), 80, CONFIG) == 0.386055

    def test_an_undated_member_counts_towards_frequency_and_not_regularity(
        self,
    ) -> None:
        """It happened — that is the frequency term — but it says nothing about
        when, so it must not join the gaps."""
        dated = _every(30, 3)

        assert automation_score([*dated, None], 80, CONFIG) > automation_score(
            dated, 80, CONFIG
        )

    def test_a_body_with_no_words_scores_zero(self) -> None:
        """Unreachable by construction — an empty ``body_clean`` hashes to zero
        and A3 discards it before clustering — but a score of zero is the right
        answer if it ever is reached."""
        assert automation_score(_every(30, 12), 0, CONFIG) == 0.0


class TestDeterminism:
    """Two rebuilds have to agree to the last digit, not to a tolerance."""

    def test_the_same_input_scores_identically_twice(self) -> None:
        sendings = _every(30, 12)

        assert automation_score(sendings, 81, CONFIG) == automation_score(
            list(reversed(sendings)), 81, CONFIG
        )

    def test_the_score_is_rounded_so_a_cypher_round_trip_cannot_move_it(
        self,
    ) -> None:
        score = automation_score(_every(29, 7), 137, CONFIG)

        assert score == round(score, 6)
        assert 0.0 <= score <= 1.0


@pytest.mark.parametrize("count", [3, 12, 40])
@pytest.mark.parametrize("words", [25, 81, 400])
def test_the_score_never_leaves_the_unit_interval(count: int, words: int) -> None:
    """Three factors in ``0..1`` multiplied stay in ``0..1``; the node's
    property is only comparable if that holds."""
    assert 0.0 <= automation_score(_every(30, count), words, CONFIG) <= 1.0
