"""The planted corpus really is planted — checked before anything reads it.

Everything the three analyses do rests on five fields the *parser* computes,
so a corpus whose ``refs`` were empty or whose ``participant_key`` grouped the
wrong messages would let a broken analysis pass and a correct one fail, and the
failure would point at the wrong file. This is the file that keeps that from
happening: it asserts what :func:`~mailarc_core.mail.parsing.parse_message`
makes of the planted bytes, so a change in the parser breaks *here*, next to
the values, rather than three test files later next to a cluster count.

Every number below was measured against this checkout and is exact. None of
them is a threshold the analyses can argue with; they are properties of these
bytes under this parser.
"""

import itertools

import corpus
import pytest
from corpus import PlantedMessage

from mailarc_core.mail.model import ParsedMessage
from mailarc_core.mail.parsing import hamming_distance, parse_message, simhash

FOOTER_ONLY = ("f1", "f2", "b1", "b2")
"""The four messages whose only shared text is the company footer."""

TEMPLATE_DISTANCE = 5
"""The default
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.simhash_max_distance`,
repeated here because this file's claims are about the bytes rather than about
a configuration — and because the negative control only means something if the
threshold it is measured against is the one A3 uses."""


@pytest.fixture(scope="module")
def parsed() -> dict[str, ParsedMessage]:
    """Every planted message through the real parser, keyed by its short name."""
    return {one.key: parse_message(one.raw) for one in corpus.planted_corpus()}


def _fingerprints(
    parsed: dict[str, ParsedMessage], keys: tuple[str, ...], *, cleaned: bool
) -> list[int]:
    return [
        parsed[key].simhash if cleaned else simhash(parsed[key].body_text)
        for key in keys
    ]


def _distances(values: list[int]) -> list[int]:
    return [
        hamming_distance(left, right)
        for left, right in itertools.combinations(values, 2)
    ]


class TestTheCorpusItself:
    """Shape and identity, before any field is looked at."""

    def test_it_holds_thirty_three_messages_in_six_blocks(self) -> None:
        planted = corpus.planted_corpus()

        assert len(planted) == 33
        assert [one.key for one in planted[:5]] == ["p1", "p2", "p3", "p4", "p5"]
        assert [one.key for one in planted[5:17]] == [f"s{n:02d}" for n in range(1, 13)]
        assert [one.key for one in planted[17:27]] == [
            f"n{n:02d}" for n in range(1, 11)
        ]
        assert [one.key for one in planted[27:]] == ["f1", "f2", "b1", "b2", "w1", "w2"]

    def test_every_message_keeps_its_own_message_id_as_the_canonical_one(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """The fixture names ``p1``; the analyses answer ``p1@nordlicht.example``.

        Only true because every planted mail carries a ``Message-ID``. Without
        one the identity falls back to a hash of the bytes and no test could
        name a message it expected to find.
        """
        assert {key: one.canonical_id for key, one in parsed.items()} == {
            key: corpus.canonical(key) for key in parsed
        }

    def test_the_bytes_are_real_rfc_5322(self) -> None:
        """Parsed rather than constructed, so the fixture cannot drift.

        A hand-built :class:`~mailarc_core.mail.model.ParsedMessage` would let
        the corpus assert a ``simhash`` the import would never produce, which
        is exactly the class of fixture bug that makes a green suite worthless.
        """
        planted: PlantedMessage = corpus.by_key()["p1"]

        assert planted.raw.startswith(b"Message-ID: <p1@nordlicht.example>")
        assert b"Content-Type: application/pdf" in planted.raw


class TestTheSignalsTheParserComputes:
    """The five analysis-bearing fields, as A2 and A3 will read them."""

    def test_only_the_project_block_carries_a_ticket_token(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """A2's strongest signal fires exactly where it was planted.

        Nothing else in the corpus may look like ``[A-Z][A-Z0-9]{1,9}-\\d+`` or
        ``#\\d{2,8}`` — not ``Nordlicht``, not ``Ausgabe 01``, not ``2026``.
        A stray token anywhere else would grow a topic nobody planted, and the
        count assertions in the topic tests would then be measuring a bug.
        """
        with_refs = {key: one.refs for key, one in parsed.items() if one.refs}

        assert with_refs == dict.fromkeys(
            ("p1", "p2", "p3", "p4", "p5"), (corpus.TICKET,)
        )

    def test_two_of_the_project_mails_carry_the_ticket_in_the_body_alone(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """``p3`` and ``p5`` are in the topic only because refs read the body.

        Their subjects normalise to something else entirely, so an
        implementation that took ``refs`` from the subject — or from
        ``body_clean``, which a quoted-only reply would have emptied — would
        find a topic of three where five were planted. The corpus is built so
        that mistake shows up as a membership assertion rather than as a
        rounding difference.
        """
        for key in ("p3", "p5"):
            assert corpus.TICKET not in parsed[key].subject
            assert corpus.TICKET in parsed[key].body_text

    def test_the_normalised_subjects_are_what_a2_compares(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """The reply prefix and the ticket are gone; the project is not."""
        assert {
            key: parsed[key].subject_norm for key in ("p1", "p2", "p3", "p4", "p5")
        } == {
            "p1": "angebot datenmigration",
            "p2": "angebot datenmigration",
            "p3": "rueckfrage zeitplan einkauf",
            "p4": "abnahmetermin",
            "p5": "protokoll steuerungsgruppe",
        }

    def test_the_two_recurring_series_normalise_to_twelve_and_ten_subjects(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """Which is what leaves A3 as the only analysis that can find them.

        If ``Statusbericht Nordlicht Januar 2026`` normalised the same way
        every month, signal 3 would join the whole block into a topic and the
        corpus would stop being able to tell A2's positive case from its
        negative one.
        """
        status = {parsed[f"s{n:02d}"].subject_norm for n in range(1, 13)}
        news = {parsed[f"n{n:02d}"].subject_norm for n in range(1, 11)}

        assert len(status) == 12
        assert len(news) == 10

    def test_the_participant_key_groups_exactly_the_planted_circles(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """One hash per circle of people, and ``f1``/``f2`` alone in theirs.

        ``b1`` and ``b2`` share a key only because
        :func:`~mailarc_core.mail.parsing.participant_key` hashes the Bcc as
        well — that is the whole reason the B block exists, and it is asserted
        here before A1 is ever asked about it.
        """
        circles: dict[str, list[str]] = {}
        for key, one in parsed.items():
            circles.setdefault(one.participant_key, []).append(key)

        assert sorted(circles.values()) == sorted(
            [
                ["p1", "p2", "p3", "p4", "p5"],
                [f"s{n:02d}" for n in range(1, 13)],
                [f"n{n:02d}" for n in range(1, 11)],
                ["f1"],
                ["f2"],
                ["b1", "b2"],
                ["w1", "w2"],
            ]
        )

    def test_one_attachment_hangs_off_two_messages(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """Content addressing is what makes a shared file an A2 signal."""
        hashes = {
            key: tuple(one.sha256 for one in message.attachments)
            for key, message in parsed.items()
            if message.attachments
        }

        assert set(hashes) == {"p1", "p4"}
        assert hashes["p1"] == hashes["p4"]
        assert len(hashes["p1"]) == 1


class TestWhatBodyCleanIsFor:
    """The negative control, stated as two numbers rather than as a hope."""

    def test_the_footer_is_gone_from_the_cleaned_body(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """Sign-off, register entry and disclaimer, all cut, all still in text."""
        marks = ("Mit freundlichen Gruessen", "HRB", "vertraulich", "Datenschutz")

        for key in FOOTER_ONLY:
            assert all(mark in parsed[key].body_text for mark in marks)
            assert not any(mark in parsed[key].body_clean for mark in marks)

    def test_over_body_text_all_four_footer_mails_are_one_template(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """Every pair of them is within A3's threshold on the *full* text.

        Measured: the six pairwise distances are 2, 2, 3, 3, 5 and 5 bits. An
        implementation that fingerprinted ``body_text`` would therefore report
        a four-message template made of a key handover, a meter reading, an
        invoice and a credit note — which is the finding ``body_clean`` exists
        to prevent, and the reason the spec calls it a precondition rather than
        a convenience.
        """
        distances = _distances(_fingerprints(parsed, FOOTER_ONLY, cleaned=False))

        assert sorted(distances) == [2, 2, 3, 3, 5, 5]
        assert max(distances) <= TEMPLATE_DISTANCE

    def test_over_body_clean_no_two_of_them_are_even_close(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """The same four, cleaned: 24 bits apart at the nearest.

        Nearly five times the threshold, so the separation is not a near miss
        that a threshold change could undo.
        """
        distances = _distances(_fingerprints(parsed, FOOTER_ONLY, cleaned=True))

        assert sorted(distances) == [24, 27, 27, 29, 31, 32]
        assert min(distances) > TEMPLATE_DISTANCE


class TestTheTwoRecurringSeries:
    """A3's positives, measured as bit distances before A3 is asked."""

    def test_the_status_block_spans_one_to_eight_bits(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """Which is why ``simhash_max_distance`` is five and not three.

        One changed month name in an eighty-word body moves up to eight bits.
        At ``<= 3`` this block breaks into four pieces — seven, three and two
        singletons — and at ``<= 5`` single linkage carries it as one. Both
        numbers are run rather than recalled, by
        ``test_derived_templates.py::TestWhyTheDistanceIsFiveAndNotThree``.
        """
        distances = _distances(
            _fingerprints(
                parsed, tuple(f"s{n:02d}" for n in range(1, 13)), cleaned=True
            )
        )

        assert (min(distances), max(distances)) == (1, 8)

    def test_the_newsletter_block_spans_two_to_nine_bits(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """A longer body and a two-digit edit; further apart, still one group."""
        distances = _distances(
            _fingerprints(
                parsed, tuple(f"n{n:02d}" for n in range(1, 11)), cleaned=True
            )
        )

        assert (min(distances), max(distances)) == (2, 9)


class TestTheSignTrapFixture:
    """The messages planted for the one bug this phase could ship silently."""

    def test_five_planted_messages_already_hash_with_the_top_bit_set(
        self, parsed: dict[str, ParsedMessage]
    ) -> None:
        """So the corpus exercises the trap even where it was not aimed at it.

        The graph stores a fingerprint signed, and roughly half of all real
        messages have bit 63 set. Naming the five here is what makes the
        round-trip test in ``test_derived_reader_local.py`` a claim about real
        data rather than about one constructed value.
        """
        negative = sorted(key for key, one in parsed.items() if one.simhash >> 63)

        assert negative == ["b2", "p4", "p5", "w1", "w2"]

    def test_the_dedicated_family_is_negative_and_within_the_threshold(self) -> None:
        """Three copies, all with the top bit set, all near enough to cluster.

        Three because a template needs three: the fixture has to be able to
        prove that a *negative* stored fingerprint still produces a template,
        not merely that it still produces a band key.
        """
        family = [parse_message(one.raw) for one in corpus.top_bit_messages()]
        distances = _distances([one.simhash for one in family])

        assert len(family) == 3
        assert all(one.simhash >> 63 for one in family)
        assert sorted(distances) == [3, 3, 4]
        assert max(distances) <= TEMPLATE_DISTANCE
