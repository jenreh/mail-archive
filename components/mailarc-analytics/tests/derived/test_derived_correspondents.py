"""A1 over the planted corpus — who is written to together, and which circles.

Two answers to one question, and they disagree about who counts. A pair is
about being *addressed together*, so the sender is out and a Bcc is out. A
group is about a circle, and ``participant_key`` was hashed over the sender, To,
Cc and Bcc, so a size counted over anything narrower would disagree with the key
it is stored under.

The corpus is built so that both halves of that have a failing case. The B
block is Bcc'd to an address that appears nowhere else, so an implementation
that folded Bcc into the pair analysis grows two edges naming somebody who was
never visibly on a message — and one that dropped Bcc from the participant set
loses a whole group, because that block only clears ``min_group_size`` if the
hidden recipient is counted. Both mistakes are demonstrated below rather than
described: the tests build the naive facts and show what A1 would then answer.
"""

from datetime import UTC, datetime

import corpus
import pytest
from corpus import ANNA, OWN, REVISION, THOMAS

from mailarc_analytics import (
    CorrespondentFindings,
    GroupFacts,
    MessageFacts,
    build_correspondents,
)

CONFIG = corpus.calibrated_config()


def _fact(
    key: str,
    *,
    addressed: tuple[str, ...] = (),
    participants: tuple[str, ...] = (),
    sent_at: datetime | None = None,
    participant_key: str = "circle",
) -> MessageFacts:
    """One hand-built message, for the claims the corpus does not plant.

    The corpus proves what A1 finds in a realistic archive; these prove the
    edges of the rules — a five-hundred-address distribution list, an undated
    message, a key whose members disagree — which no corpus should have to
    contain in order to be readable.
    """
    return MessageFacts(
        id=key,
        addressed=addressed,
        participants=participants or addressed,
        sent_at=sent_at,
        participant_key=participant_key,
    )


def _group(found: CorrespondentFindings, key: str) -> GroupFacts:
    """The group a planted message belongs to, looked up by that message.

    By key rather than by position: the groups come back sorted by their
    ``participant_key``, which is a sha256 and therefore in an order nobody
    should be reading meaning into.
    """
    circle = corpus.circle_of(key)
    return next(one for one in found.groups if one.id == circle)


@pytest.fixture(scope="module")
def found() -> CorrespondentFindings:
    """A1 over the whole planted corpus."""
    return build_correspondents(corpus.planted_facts(), CONFIG)


class TestTheCoAddressedPairs:
    """Exactly three, and exactly these three."""

    def test_the_corpus_yields_three_pairs_and_no_others(
        self, found: CorrespondentFindings
    ) -> None:
        """All three come from the project block; nothing else in the corpus
        has two visible recipients on one message."""
        assert [(one.left, one.right, one.count) for one in found.pairs] == [
            (ANNA, OWN, 1),
            (ANNA, THOMAS, 2),
            (OWN, THOMAS, 2),
        ]

    def test_each_pair_carries_the_window_its_messages_span(
        self, found: CorrespondentFindings
    ) -> None:
        """First and last seen are the messages' dates, not the run's."""
        windows = {
            (one.left, one.right): (one.first_seen, one.last_seen)
            for one in found.pairs
        }

        assert windows[(ANNA, THOMAS)] == (
            datetime(2026, 1, 12, 9, 0, tzinfo=UTC),
            datetime(2026, 2, 3, 7, 45, tzinfo=UTC),
        )
        assert windows[(OWN, THOMAS)] == (
            datetime(2026, 1, 13, 11, 30, tzinfo=UTC),
            datetime(2026, 2, 10, 16, 0, tzinfo=UTC),
        )

    def test_the_endpoints_are_in_canonical_order(
        self, found: CorrespondentFindings
    ) -> None:
        """One unordered pair is one edge, so the write needs one order.

        Which way round the edge is physically stored is an accident of who was
        written to first, which is why every read has to match it without an
        arrow.
        """
        assert all(one.left < one.right for one in found.pairs)

    def test_the_sender_is_never_paired_with_their_own_recipients(self) -> None:
        """Otherwise the heaviest edge in any archive is "the user, and
        everyone the user has ever mailed" — a fact already available as the
        degree of ``SENT_FROM``, and one that buries the finding."""
        one_sided = _fact("m", addressed=(ANNA,), participants=(ANNA, OWN))

        pairs = build_correspondents([one_sided], CONFIG).pairs

        assert pairs == ()

    def test_an_undated_message_still_counts_but_dates_nothing(self) -> None:
        """It happened; it just has nothing to say about when."""
        undated = _fact("m", addressed=(ANNA, THOMAS))

        pair = build_correspondents([undated], CONFIG).pairs[0]

        assert (pair.count, pair.first_seen, pair.last_seen) == (1, None, None)


class TestTheBccControl:
    """The confidentiality a derived edge must not undo."""

    def test_no_pair_names_the_address_that_was_only_ever_blind_copied(
        self, found: CorrespondentFindings
    ) -> None:
        """A Bcc recipient was written to *without* the others knowing.

        An edge between them would materialise into a finding exactly what the
        header exists to hide, and a human reading the graph later would see
        "these two work together".
        """
        named = {one.left for one in found.pairs} | {one.right for one in found.pairs}

        assert REVISION not in named

    def test_folding_the_bcc_into_the_recipients_would_invent_two_edges(self) -> None:
        """The mistake, executed, so the control is not merely asserted absent.

        Feed A1 the same corpus with the hidden recipient moved into the
        addressed set and it draws a pair for every Bcc'd message.
        """
        naive = tuple(
            one.model_copy(update={"addressed": one.participants})
            for one in corpus.planted_facts()
        )

        pairs = build_correspondents(naive, CONFIG).pairs

        assert [
            (one.left, one.right, one.count)
            for one in pairs
            if REVISION in (one.left, one.right)
        ] == [
            (ANNA, REVISION, 2),
            (OWN, REVISION, 2),
        ]


class TestTheGroups:
    """Exactly two, and the second exists only because the Bcc was counted."""

    def test_the_corpus_yields_two_groups_and_no_others(
        self, found: CorrespondentFindings
    ) -> None:
        """The project circle and the invoice circle.

        Everything else is filtered on purpose: the status mails and the
        newsletter go to one recipient each, so their circles are pairs; ``w1``
        and ``w2`` are a pair too; ``f1`` and ``f2`` have a circle each and one
        message in it.
        """
        assert {one.id: (one.size, one.message_count) for one in found.groups} == {
            corpus.circle_of("p1"): (3, 5),
            corpus.circle_of("b1"): (3, 2),
        }

    def test_the_project_group_holds_exactly_the_project_block(
        self, found: CorrespondentFindings
    ) -> None:
        project = _group(found, "p1")

        assert project.members == tuple(
            corpus.canonical(f"p{n}") for n in (1, 2, 3, 4, 5)
        )
        assert project.first_seen == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        assert project.last_seen == datetime(2026, 2, 10, 16, 0, tzinfo=UTC)

    def test_the_group_is_keyed_by_the_participant_key_itself(
        self, found: CorrespondentFindings
    ) -> None:
        """No clique search: the import already hashed the circle, and the
        group is what falls out of counting by that hash."""
        assert _group(found, "p1").id == corpus.circle_of("p1")

    def test_the_blind_copied_group_counts_three_addresses_for_two_visible(
        self, found: CorrespondentFindings
    ) -> None:
        """The B block's whole point: sender, one recipient, one hidden one."""
        invoices = _group(found, "b1")

        assert invoices.size == 3
        assert invoices.members == (corpus.canonical("b1"), corpus.canonical("b2"))

    def test_dropping_the_bcc_from_the_participants_would_lose_that_group(
        self,
    ) -> None:
        """The other half of the mistake, also executed.

        With the hidden recipient out of the participant set the invoice circle
        is two addresses, ``min_group_size`` filters it away, and A1 reports
        one group where two were planted — a finding lost silently.
        """
        naive = tuple(
            one.model_copy(
                update={
                    "participants": tuple(sorted({one.sender, *one.addressed} - {""}))
                }
            )
            for one in corpus.planted_facts()
        )

        groups = build_correspondents(naive, CONFIG).groups

        assert len(groups) == 1
        assert groups[0].id == corpus.circle_of("p1")

    def test_a_circle_below_the_size_threshold_is_not_a_group(self) -> None:
        """Two people who write is a pair, and ``CO_ADDRESSED`` answers that
        question better than a node would."""
        facts = [
            _fact(f"m{n}", addressed=(ANNA,), participants=(ANNA, OWN))
            for n in range(5)
        ]

        assert build_correspondents(facts, CONFIG).groups == ()

    def test_a_circle_that_wrote_once_is_not_a_group(self) -> None:
        """One message to three people is a mail, not a working group."""
        once = [_fact("m", addressed=(ANNA, THOMAS), participants=(ANNA, OWN, THOMAS))]

        assert build_correspondents(once, CONFIG).groups == ()

    def test_a_message_without_a_participant_key_joins_no_group(self) -> None:
        """A message nobody could be read off must not group with every other
        one that also failed to parse."""
        facts = [
            _fact(f"m{n}", addressed=(ANNA, THOMAS), participant_key="")
            for n in range(3)
        ]

        assert build_correspondents(facts, CONFIG).groups == ()


class TestTheRecipientCap:
    """One all-hands mail is a hundred and twenty-five thousand edges."""

    def test_a_message_above_the_cap_contributes_no_pair(self) -> None:
        crowd = tuple(
            f"person{n:03d}@example.com"
            for n in range(CONFIG.co_addressed_max_recipients + 1)
        )

        found = build_correspondents([_fact("m", addressed=crowd)], CONFIG)

        assert found.pairs == ()
        assert found.wide_messages == 1

    def test_a_message_exactly_at_the_cap_still_contributes(self) -> None:
        """The threshold is a ceiling, not a fence one short of it."""
        crowd = tuple(
            f"person{n:03d}@example.com"
            for n in range(CONFIG.co_addressed_max_recipients)
        )

        found = build_correspondents([_fact("m", addressed=crowd)], CONFIG)

        assert len(found.pairs) == 25 * 24 // 2
        assert found.wide_messages == 0

    def test_a_capped_message_still_counts_towards_its_group(self) -> None:
        """One node absorbs the whole distribution list; the pairs are what
        would have buried the finding."""
        crowd = tuple(f"person{n:03d}@example.com" for n in range(40))
        facts = [
            _fact(f"m{n}", addressed=crowd, participants=(*crowd, OWN))
            for n in range(2)
        ]

        found = build_correspondents(facts, CONFIG)

        assert found.pairs == ()
        assert found.wide_messages == 2
        assert [(one.size, one.message_count) for one in found.groups] == [(41, 2)]


class TestTheAnswerIsStable:
    """Two rebuilds have to write the same rows, in the same order."""

    def test_the_findings_do_not_depend_on_the_order_of_the_facts(self) -> None:
        facts = corpus.planted_facts()

        assert build_correspondents(facts, CONFIG) == build_correspondents(
            tuple(reversed(facts)), CONFIG
        )

    def test_a_disagreeing_participant_key_takes_the_widest_set(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A key is a hash of a set, so two sizes under one key is a bug.

        Reported and then survived, because the derived layer is disposable and
        one odd key must not cost a whole rebuild — the same reason the status
        panel survives one unreadable graph.
        """
        facts = [
            _fact("m1", addressed=(ANNA,), participants=(ANNA, OWN)),
            _fact("m2", addressed=(ANNA,), participants=(ANNA, OWN, THOMAS)),
        ]

        with caplog.at_level("WARNING"):
            groups = build_correspondents(facts, CONFIG).groups

        assert [one.size for one in groups] == [3]
        assert "describes sets of" in caplog.text
