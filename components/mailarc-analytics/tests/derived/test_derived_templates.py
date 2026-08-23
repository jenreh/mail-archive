"""A3 over the planted corpus — two templates, and what keeps them from being
three.

The corpus plants one series in each direction: twelve monthly status reports
the archive's owner wrote, ten newsletters that arrived. A3 has to find exactly
those two, keep them apart because only what you write yourself is automatable,
and rank the one anybody could act on above the one nobody can.

It also plants four messages that share nothing but a company footer. On the
*full* text every pair of them is within the template threshold, so an
implementation that fingerprinted ``body_text`` reports them — together with a
project mail that also carries the footer — as a five-message template. That
run is executed below rather than described, because "``body_clean`` is a
precondition, not a convenience" is a claim that only means something with the
counterexample next to it.

The last group of tests is the sign trap. A fingerprint with bit 63 set is
stored negative, and a Hamming distance taken across a sign boundary answers 62
where the truth is 4. The failure is not wrong templates — it is *no* templates,
which looks exactly like an archive with nothing repetitive in it.
"""

import hashlib

import corpus
import pytest

from mailarc_analytics import (
    MessageFacts,
    TemplateCluster,
    TemplateDirection,
    describe_templates,
    group_templates,
    template_id,
)
from mailarc_core.archive.model import to_signed_64
from mailarc_core.mail.parsing import hamming_distance

CONFIG = corpus.calibrated_config()

SENT = "template:1e164feec6258562:sent"
RECEIVED = "template:132b71d16ae83c39:received"

STATUS = tuple(corpus.canonical(f"s{n:02d}") for n in range(1, 13))
NEWSLETTER = tuple(corpus.canonical(f"n{n:02d}") for n in range(1, 11))
FOOTER_ONLY = ("f1", "f2", "b1", "b2")

SIGN_BIT = 1 << 63


def _synthetic(index: int) -> int:
    """A fingerprint nothing else in a test resembles, reproducibly.

    Two digests of different numbers sit about thirty-two bits apart, which is
    six times A3's threshold, so a hundred of these are a hundred singletons.
    Only ever used where the claim is about a *proportion* of an archive rather
    than about what a body hashes to.
    """
    return int.from_bytes(hashlib.sha256(str(index).encode()).digest()[:8], "big")


def _described(facts: tuple[MessageFacts, ...]) -> tuple[TemplateCluster, ...]:
    """Both halves of A3 over the same facts, with the bodies read eagerly."""
    grouping = group_templates(facts, CONFIG)
    return describe_templates(grouping, {one.id: one for one in facts}, {}, CONFIG)


@pytest.fixture(scope="module")
def found() -> tuple[TemplateCluster, ...]:
    """A3 over the whole planted corpus."""
    return _described(corpus.planted_facts())


def _by_id(found: tuple[TemplateCluster, ...], key: str) -> TemplateCluster:
    return next(one for one in found if one.id == key)


class TestTheTwoTemplatesThatWerePlanted:
    """Exact count, exact membership, exact key."""

    def test_the_corpus_yields_two_templates(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        assert len(found) == 2

    def test_they_are_keyed_by_fingerprint_and_direction(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        """Sixteen hex characters of the representative's fingerprint, unsigned,
        and the direction — the only key under which a rebuild is a no-op."""
        assert sorted(one.id for one in found) == [RECEIVED, SENT]

    def test_the_sent_template_is_the_twelve_status_reports(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        """All twelve, although they are up to eight bits apart.

        Single linkage is what carries that: the group only needs eleven of its
        sixty-six pairs, and four sixteen-bit bands supply far more than eleven
        even at a per-pair recall of three quarters.
        """
        template = _by_id(found, SENT)

        assert template.direction is TemplateDirection.SENT
        assert tuple(one.message_id for one in template.members) == STATUS

    def test_the_received_template_is_the_ten_newsletters(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        template = _by_id(found, RECEIVED)

        assert template.direction is TemplateDirection.RECEIVED
        assert tuple(one.message_id for one in template.members) == NEWSLETTER

    def test_nothing_else_in_the_corpus_is_in_a_template(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        members = {one.message_id for template in found for one in template.members}

        assert members == set(STATUS) | set(NEWSLETTER)

    def test_every_member_is_measured_against_the_representative(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        """The representative is the smallest canonical id, so it is the one
        member at distance zero and the one whose fingerprint is the key."""
        template = _by_id(found, SENT)
        zero = [one.message_id for one in template.members if one.distance == 0]

        assert zero == [corpus.canonical("s01")]
        assert (
            max(one.distance for one in template.members)
            <= CONFIG.simhash_max_distance * 2
        )


class TestWhatTheScoreSays:
    """The ranking §6.3 asks for, in the two numbers it comes out as."""

    def test_the_monthly_report_out_ranks_the_newsletter_more_than_twofold(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        """Both recur; only one recurs on a schedule and is short enough to be
        a form. That gap is the whole point of multiplying the three factors
        rather than averaging them."""
        assert _by_id(found, SENT).automation_score == 0.641072
        assert _by_id(found, RECEIVED).automation_score == 0.279724
        assert (
            _by_id(found, SENT).automation_score
            > 2 * _by_id(found, RECEIVED).automation_score
        )

    def test_each_template_carries_the_window_its_members_span(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        template = _by_id(found, SENT)

        assert template.first_seen is not None
        assert template.last_seen is not None
        assert (template.first_seen.month, template.last_seen.month) == (1, 12)

    def test_the_sample_is_the_shortest_member_truncated(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        """Shortest rather than first: it is the copy with the least filled in,
        so it shows the form with the fewest of its variables.

        Here that is the May report — the shortest month name in the block.
        """
        template = _by_id(found, SENT)

        assert "fuer Mai." in template.sample_text
        assert len(template.sample_text) == CONFIG.template_sample_length

    def test_the_sample_never_contains_the_footer(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        """It is taken from ``body_clean``, so what a human sees is what the
        clustering actually compared."""
        for template in found:
            assert "Mit freundlichen Gruessen" not in template.sample_text


class TestTheDirectionSplit:
    """Sent and received are two passes, not one pass with a label."""

    def test_no_template_mixes_the_two_directions(
        self, found: tuple[TemplateCluster, ...]
    ) -> None:
        sent = {one.message_id for one in _by_id(found, SENT).members}
        received = {one.message_id for one in _by_id(found, RECEIVED).members}

        assert sent.isdisjoint(received)

    def test_the_same_text_arriving_both_ways_is_two_templates(self) -> None:
        """A newsletter the user also forwards to themselves is two findings:
        one of them is theirs to automate and one is not, and a single node
        could not say which."""
        planted = corpus.top_bit_messages()
        outbound = [corpus.facts_of(one) for one in planted]
        inbound = [
            corpus.facts_of(one).model_copy(
                update={"id": f"in-{one.key}", "outbound": False}
            )
            for one in planted
        ]

        found = _described(tuple(outbound + inbound))

        assert {one.direction for one in found} == {
            TemplateDirection.SENT,
            TemplateDirection.RECEIVED,
        }
        assert len(found) == 2

    def test_a_direction_with_too_few_copies_yields_nothing(self) -> None:
        """Twice is a coincidence — ``template_min_occurrences`` is three."""
        two = tuple(corpus.facts_of(one) for one in corpus.top_bit_messages()[:2])

        assert _described(two) == ()


class TestTheFooterControl:
    """The negative case ``body_clean`` exists for, run both ways."""

    @pytest.mark.parametrize("key", FOOTER_ONLY)
    def test_no_footer_sharing_message_reaches_a_template(
        self, found: tuple[TemplateCluster, ...], key: str
    ) -> None:
        members = {one.message_id for template in found for one in template.members}

        assert corpus.canonical(key) not in members

    def test_fingerprinting_the_full_text_would_invent_a_five_message_template(
        self,
    ) -> None:
        """The mistake, executed on the same corpus.

        Four messages that share only a footer, plus a project mail that
        carries the same one, come back as one template of five — a key
        handover, a meter reading, an invoice, a credit note and an offer,
        reported as a text somebody keeps retyping.
        """
        naive = _described(corpus.planted_facts(fingerprint_body_text=True))
        bogus = [
            one
            for one in naive
            if corpus.canonical("f1") in {member.message_id for member in one.members}
        ]

        assert len(bogus) == 1
        assert {member.message_id for member in bogus[0].members} == {
            corpus.canonical(key) for key in (*FOOTER_ONLY, "p1")
        }

    def test_the_two_recurring_series_survive_the_naive_run_too(self) -> None:
        """So the counterexample is about the footer and not about the hash.

        The status block and the newsletter block are found either way; what
        the cleaning changes is the third, spurious finding.
        """
        naive = _described(corpus.planted_facts(fingerprint_body_text=True))

        assert sorted(one.occurrences for one in naive) == [5, 10, 12]


class TestMessagesWithNothingToFingerprint:
    """An empty body hashes to zero, and everything empty is alike."""

    def test_they_are_discarded_rather_than_grouped(self) -> None:
        """One bucket holding every quoted-only reply in the archive would
        otherwise be the highest-scoring finding in it."""
        empty = tuple(
            corpus.facts_of(one).model_copy(update={"simhash": 0, "body_clean": ""})
            for one in corpus.top_bit_messages()
        )

        grouping = group_templates(empty, CONFIG)

        assert grouping.groups == ()
        assert grouping.unhashable_messages == 3

    def test_the_planted_corpus_has_nothing_unhashable(self) -> None:
        """Every planted mail has a body worth fingerprinting, so the two
        template counts above are not a count of what was left over."""
        assert group_templates(corpus.planted_facts(), CONFIG).unhashable_messages == 0


class TestTheSignTrap:
    """A stored fingerprint is negative half the time; A3 must not notice."""

    def test_a_family_whose_fingerprints_all_have_the_top_bit_set_clusters(
        self,
    ) -> None:
        """Three copies, all negative once stored, one template.

        The corpus plants this family precisely because the two recurring
        series happen to hash positive: without it, the whole of A3 would be
        tested on the easy half of the value range.
        """
        facts = tuple(corpus.facts_of(one) for one in corpus.top_bit_messages())

        found = _described(facts)

        assert all(one.simhash >> 63 for one in facts)
        assert len(found) == 1
        assert found[0].occurrences == 3

    def test_the_key_of_such_a_template_carries_no_minus_sign(self) -> None:
        """``f"{stored:016x}"`` on a negative value emits ``-50a6dea0…``, and
        two runs disagreeing about a minus sign are not a key."""
        found = _described(
            tuple(corpus.facts_of(one) for one in corpus.top_bit_messages())
        )

        facts = tuple(corpus.facts_of(one) for one in corpus.top_bit_messages())
        representative = min(facts, key=lambda one: one.id)

        assert "-" not in found[0].id
        assert found[0].id == "template:a0b86145044638a0:sent"
        assert found[0].id == template_id(
            representative.simhash, TemplateDirection.SENT
        )

    def test_a_template_that_straddles_the_sign_is_still_one_template(self) -> None:
        """The case that actually breaks: some copies negative, some positive.

        Built by flipping bit 63 on two of the three planted fingerprints, so
        the three are four or five bits apart as unsigned values and two of
        them now store positive. A3 has to see one group.
        """
        base = [corpus.facts_of(one) for one in corpus.top_bit_messages()]
        straddling = (
            base[0],
            *(
                one.model_copy(update={"simhash": one.simhash ^ SIGN_BIT})
                for one in base[1:]
            ),
        )
        signs = {one.simhash >> 63 for one in straddling}

        found = _described(straddling)

        assert signs == {0, 1}
        assert len(found) == 1
        assert found[0].occurrences == 3

    def test_the_same_family_read_back_signed_would_find_no_template_at_all(
        self,
    ) -> None:
        """Which is why the reader converts, once, at the boundary.

        A mixed-sign Hamming distance answers twenty-eight where the truth is
        four, so the copies never become candidates and A3 reports an archive
        with nothing repetitive in it. That is a much quieter failure than a
        wrong cluster, and this is the assertion that would catch it.
        """
        base = [corpus.facts_of(one) for one in corpus.top_bit_messages()]
        straddling = [
            base[0],
            *(
                one.model_copy(update={"simhash": one.simhash ^ SIGN_BIT})
                for one in base[1:]
            ),
        ]
        unconverted = tuple(
            one.model_copy(update={"simhash": to_signed_64(one.simhash)})
            for one in straddling
        )

        assert hamming_distance(straddling[0].simhash, straddling[1].simhash) == 5
        assert hamming_distance(unconverted[0].simhash, unconverted[1].simhash) == 28
        assert _described(unconverted) == ()


class TestTheTwoWaysOfReadingTheBodies:
    """Eagerly with the facts, or late by id — both must give one answer."""

    def test_a_late_body_read_produces_the_same_templates(self) -> None:
        """The bounded path a large archive takes: cluster on fingerprints,
        then fetch the text of the few hundred messages that ended up in a
        template. It must not be a different analysis."""
        facts = corpus.planted_facts()
        stripped = tuple(one.model_copy(update={"body_clean": ""}) for one in facts)
        bodies = {one.id: one.body_clean for one in facts}
        grouping = group_templates(stripped, CONFIG)

        late = describe_templates(
            grouping, {one.id: one for one in stripped}, bodies, CONFIG
        )

        assert late == _described(facts)

    def test_a_group_whose_bodies_are_all_missing_scores_zero(self) -> None:
        """Only reachable when the graph lost the text between two reads.
        Losing the sample is a better outcome than losing the rebuild."""
        facts = tuple(
            corpus.facts_of(one).model_copy(update={"body_clean": ""})
            for one in corpus.top_bit_messages()
        )
        grouping = group_templates(facts, CONFIG)

        found = describe_templates(grouping, {one.id: one for one in facts}, {}, CONFIG)

        assert found[0].sample_text == ""
        assert found[0].automation_score == 0.0


class TestTheRunawayGuard:
    """Single linkage can chain across an archive; a chain is not a finding."""

    def test_a_template_holding_a_twentieth_of_a_direction_is_warned_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Warned about and kept, because throwing it away would hide the
        calibration problem instead of showing it.

        The hundred fingerprints here are derived from a digest rather than
        from text, which is the one place in this suite that is true: the claim
        is about a proportion of a direction, and planting a hundred realistic
        mails to state it would say nothing the generator does not.
        """
        crowd = tuple(
            MessageFacts(id=f"m{index:04d}", simhash=_synthetic(index), outbound=True)
            for index in range(94)
        )
        template = tuple(
            MessageFacts(
                id=f"t{index:04d}", simhash=_synthetic(0) ^ (1 << index), outbound=True
            )
            for index in range(6)
        )

        with caplog.at_level("WARNING"):
            found = _described(crowd + template)

        assert len(found) == 1
        assert found[0].occurrences == 7
        assert "check the distance threshold" in caplog.text

    def test_a_small_archive_never_earns_the_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A test corpus of thirty is *supposed* to be twelve monthly reports
        and ten newsletters, and a warning on every such run is a warning
        nobody reads by the time a real archive earns one."""
        with caplog.at_level("WARNING"):
            _described(corpus.planted_facts())

        assert "check the distance threshold" not in caplog.text


class TestWhyTheDistanceIsFiveAndNotThree:
    """The one deviation from §6.3, executed rather than recalled.

    ``simhash_max_distance`` is five where the spec says three, and the whole
    argument for it is a measurement on this corpus. A justification nothing
    runs is a justification that rots: the next reader restoring the spec value
    would find every test still green, because no test asks the analyses what
    happens at three.
    """

    @pytest.mark.parametrize(
        ("distance", "occurrences"),
        [(3, [3, 6, 7]), (4, [10, 10]), (5, [10, 12])],
    )
    def test_the_two_planted_series_only_survive_whole_at_five(
        self, distance: int, occurrences: list[int]
    ) -> None:
        """Three splits both series, four silently drops two members of one,
        five carries both."""
        config = CONFIG.model_copy(update={"simhash_max_distance": distance})

        grouping = group_templates(corpus.planted_facts(), config)

        assert sorted(len(one.members) for one in grouping.groups) == occurrences

    def test_at_three_the_monthly_series_breaks_into_four_pieces(self) -> None:
        """Seven, three and two singletons — the number both
        ``AnalyticsConfig.simhash_max_distance`` and ``test_derived_corpus.py``
        quote. Two of them fall below ``template_min_occurrences``, which is
        why the analysis reports two templates for a block that fell into
        four."""
        config = CONFIG.model_copy(
            update={"simhash_max_distance": 3, "template_min_occurrences": 1}
        )
        status = tuple(one for one in corpus.planted_facts() if one.id in set(STATUS))

        grouping = group_templates(status, config)

        assert sorted(len(one.members) for one in grouping.groups) == [1, 1, 3, 7]

    def test_at_three_two_status_reports_belong_to_no_template_at_all(self) -> None:
        """Named, so a failure says which month stopped chaining."""
        config = CONFIG.model_copy(update={"simhash_max_distance": 3})

        grouping = group_templates(corpus.planted_facts(), config)

        grouped = {
            member.message_id for one in grouping.groups for member in one.members
        }
        assert set(STATUS) - grouped == {
            corpus.canonical("s05"),
            corpus.canonical("s09"),
        }


class TestTheComparisonBudget:
    """What keeps one band bucket from costing an afternoon.

    Comparing a bucket of *k* distinct fingerprints is ``k(k-1)/2`` Hamming
    distances and nothing bounds *k* on its own. Measured: eight thousand
    unrelated fingerprints sharing one band cost 6.5 seconds where eight
    thousand near-duplicates cost 0.18, because a pair already in one component
    skips the distance. So the budget is spent on *comparisons* rather than
    capped per bucket — a cap would throw away the large families A3 exists to
    find, and those are exactly the cheap ones.
    """

    def _crowd(self, count: int, *, band: int = 0xBEEF) -> tuple[MessageFacts, ...]:
        """*count* fingerprints that share band 0 and nothing above it."""
        return tuple(
            MessageFacts(
                id=f"m{index:05d}",
                simhash=(_synthetic(index) >> 16 << 16) | band,
                outbound=True,
            )
            for index in range(count)
        )

    def test_a_bucket_the_budget_cannot_afford_is_skipped_and_counted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Skipped rather than compared, and reported rather than silent —
        every other omission in this package is a number the job row can show.
        """
        facts = self._crowd(60)
        stingy = CONFIG.model_copy(update={"template_max_comparisons": 10})

        with caplog.at_level("WARNING"):
            grouping = group_templates(facts, stingy)

        assert grouping.dropped_buckets == 1
        assert grouping.groups == ()
        assert "comparison budget" in caplog.text

    def test_the_copies_in_a_skipped_bucket_are_still_one_template(self) -> None:
        """Identical fingerprints are chained before any budget is consulted:
        they are at distance zero by definition, so joining them costs nothing
        and a skipped bucket must not cost an archive its byte-identical
        newsletters."""
        facts = (
            *self._crowd(60),
            *(
                MessageFacts(id=f"same{index}", simhash=0x1234_5678_9ABC_BEEF)
                for index in range(3)
            ),
        )
        stingy = CONFIG.model_copy(update={"template_max_comparisons": 10})

        grouping = group_templates(facts, stingy)

        assert grouping.dropped_buckets == 1
        assert [len(one.members) for one in grouping.groups] == [3]

    def test_a_near_duplicate_family_is_never_what_the_budget_stops(self) -> None:
        """The discrimination the whole design rests on: a genuine family is
        the cheap case, because after the first unions every further pair is
        already in one component and skips its distance."""
        family = tuple(
            MessageFacts(
                id=f"f{index:05d}",
                simhash=0xA5A5_A5A5_A5A5_BEEF ^ (1 << (16 + index % 40)),
                outbound=True,
            )
            for index in range(40)
        )

        grouping = group_templates(family, CONFIG)

        assert grouping.dropped_buckets == 0
        assert [len(one.members) for one in grouping.groups] == [40]

    def test_the_planted_corpus_never_reaches_the_budget(self) -> None:
        """The default is four hundred times what real German business mail
        was measured to need, so the two counts above are findings and not
        leftovers."""
        assert group_templates(corpus.planted_facts(), CONFIG).dropped_buckets == 0

    def test_the_smallest_buckets_are_spent_on_first(self) -> None:
        """The same triage ``_join_weak`` does. A four-message bucket is a
        form; the bucket that swallowed a fifth of the archive is not, and it
        must not be the one that gets the budget."""
        facts = (
            *self._crowd(60, band=0xBEEF),
            *(
                MessageFacts(
                    id=f"pair{index}",
                    simhash=0x0F0F_0F0F_0F0F_1234 ^ (index << 20),
                    outbound=True,
                )
                for index in range(3)
            ),
        )
        stingy = CONFIG.model_copy(update={"template_max_comparisons": 100})

        grouping = group_templates(facts, stingy)

        assert [len(one.members) for one in grouping.groups] == [3]
        assert grouping.dropped_buckets == 1


def test_the_answer_does_not_depend_on_the_order_of_the_facts() -> None:
    """Single linkage is order-independent, and so the key has to be."""
    facts = corpus.planted_facts()

    assert _described(tuple(reversed(facts))) == _described(facts)
