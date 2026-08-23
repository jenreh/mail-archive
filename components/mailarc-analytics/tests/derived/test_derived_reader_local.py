"""The reader against a real graph — the one place the two paths must agree.

Every pure test above works from :func:`corpus.facts_of`, which projects a
parsed message the way the reader projects an archived one. That is a second
implementation, and a second implementation is only useful if something checks
it against the first. This file is that check: it archives the planted corpus
with the real writer, reads it back with the real reader, and asserts the two
tuples are equal field for field.

It is also where the sign trap is finally a claim about a database rather than
about arithmetic. The writer stores ``to_signed_64(simhash)`` because every
Cypher backend's integer is signed 64-bit, five of the planted messages come
back negative, and ``MessageFacts.simhash`` has to be unsigned all the same.
"""

from datetime import UTC, datetime

import corpus
import pytest
from planted_graph import ground_truth

from mailarc_analytics import (
    AnalyticsConfig,
    MessageFacts,
    count_unidentified,
    read_account_addresses,
    read_bodies,
    read_facts,
)
from mailarc_analytics.derived import reader
from mailarc_core.archive.model import to_signed_64
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

CONFIG = corpus.calibrated_config()

NEGATIVE = ("b2", "p4", "p5", "w1", "w2")
"""The planted messages whose fingerprint has bit 63 set.

Named rather than counted, so a failure says which message stopped being
negative — which is a change in the parser, not in the reader.
"""


def _facts(
    config: GraphConfig, analytics: AnalyticsConfig = CONFIG
) -> tuple[MessageFacts, ...]:
    with client.session(config) as graph:
        return read_facts(graph, analytics)


class TestTheRoundTrip:
    """What the writer wrote is what the analyses read."""

    def test_every_planted_message_comes_back(self, archived: GraphConfig) -> None:
        assert len(_facts(archived)) == 33

    def test_the_facts_arrive_in_canonical_id_order(
        self, archived: GraphConfig
    ) -> None:
        """Not decoration: a capped read without an order is an arbitrary
        subset, and two rebuilds would then cluster different messages."""
        found = _facts(archived)

        assert [one.id for one in found] == sorted(one.id for one in found)

    def test_they_match_the_projection_the_pure_tests_use(
        self, archived: GraphConfig
    ) -> None:
        """Field for field, ``body_clean`` excepted — the reader leaves the
        text behind on purpose and fetches it later by id.

        This is the assertion that makes every pure test above a statement
        about the real archive rather than about a fixture.
        """
        found = {
            one.id: one.model_dump(exclude={"body_clean"}) for one in _facts(archived)
        }
        projected = {
            one.id: one.model_dump(exclude={"body_clean"})
            for one in corpus.planted_facts()
        }

        assert found == projected

    def test_the_first_read_carries_no_message_text(
        self, archived: GraphConfig
    ) -> None:
        """``body_clean`` is uncapped, and reading it for a hundred thousand
        messages puts the archive's text beside an in-process FalkorDB."""
        assert {one.body_clean for one in _facts(archived)} == {""}

    def test_the_bodies_come_back_when_they_are_asked_for_by_id(
        self, archived: GraphConfig
    ) -> None:
        """A3's second read, bounded by its findings rather than by the archive."""
        wanted = [corpus.canonical("s01"), corpus.canonical("n01")]

        with client.session(archived) as graph:
            bodies = read_bodies(graph, wanted)

        assert set(bodies) == set(wanted)
        assert bodies[corpus.canonical("s01")].startswith("Hallo zusammen,")
        assert "Mit freundlichen Gruessen" not in bodies[corpus.canonical("s01")]

    def test_asking_for_a_message_that_is_not_there_returns_nothing_for_it(
        self, archived: GraphConfig
    ) -> None:
        """The caller falls back to what it already holds, not to a guess."""
        with client.session(archived) as graph:
            bodies = read_bodies(graph, ["nobody@nordlicht.example"])

        assert bodies == {}

    def test_a_message_whose_body_the_cleaner_emptied_is_absent_rather_than_blank(
        self, archived: GraphConfig
    ) -> None:
        """A quote-only reply is real mail and leaves ``body_clean`` empty.

        Absent and not present-with-an-empty-value, because A3's ``_body``
        falls back to whatever the facts already carry — and an empty string
        handed over as an answer would win that fallback and become the
        template's sample text.
        """
        with client.session(archived) as graph:
            graph.execute(
                "CREATE (m:Message {id: $id, body_clean: '', simhash: 0, refs: []})",
                {"id": "quoted@nordlicht.example"},
            )
            bodies = read_bodies(graph, ["quoted@nordlicht.example"])

        assert bodies == {}


class TestAddressCase:
    """The nodes are lowercase, and the reader may not assume they will be.

    Every address in the planted corpus is already lowercase, because the
    parser normalises on the way in — which means the lowering in
    ``_address_set`` and in ``read_account_addresses`` normalises something the
    fixture never varies. These two write the node directly to vary it, because
    the consequence of getting it wrong is not a formatting difference: two
    spellings of one address are two endpoints of ``CO_ADDRESSED`` and split
    every pair count in half, and an account address that fails to match makes
    all of the user's own mail read as received.
    """

    def test_a_recipient_stored_mixed_case_comes_back_lowercased(
        self, archived: GraphConfig
    ) -> None:
        with client.session(archived) as graph:
            graph.execute(
                "CREATE (m:Message {id: $id, simhash: 0, refs: []}) "
                "CREATE (a:Address {id: 'Anna.MEIER@Kunde.Example'}) "
                "CREATE (m)-[:SENT_TO]->(a)",
                {"id": "mixed@nordlicht.example"},
            )
            found = read_facts(graph, CONFIG)

        mixed = next(one for one in found if one.id == "mixed@nordlicht.example")
        assert mixed.addressed == ("anna.meier@kunde.example",)
        assert mixed.participants == ("anna.meier@kunde.example",)

    def test_an_account_address_stored_mixed_case_still_owns_its_own_mail(
        self, archived: GraphConfig
    ) -> None:
        """Otherwise A3 reports zero *sent* templates on the whole archive,
        which reads exactly like a user who writes nothing repetitive."""
        with client.session(archived) as graph:
            graph.execute(
                "MATCH (a:Account) SET a.address = $address",
                {"address": "Jens@Nordlicht.Example"},
            )
            owned = read_account_addresses(graph)
            found = read_facts(graph, CONFIG)

        assert owned == frozenset({corpus.OWN})
        assert len([one for one in found if one.outbound]) == 19


class TestTheSignTrapAcrossTheGraph:
    """Signed on the way in, unsigned on the way out, once, at the boundary."""

    def test_the_graph_really_stores_some_fingerprints_negative(
        self, archived: GraphConfig
    ) -> None:
        """Not a hypothetical: five of thirty-three planted messages.

        On a real archive it is roughly half, which is why reading the stored
        value and handing it on is the single most expensive mistake available
        in this phase.
        """
        with client.session(archived) as graph:
            rows = graph.execute(
                "MATCH (m:Message) WHERE m.simhash < 0 RETURN m.id ORDER BY m.id"
            ).rows

        assert [row[0] for row in rows] == [corpus.canonical(key) for key in NEGATIVE]

    def test_the_reader_hands_every_fingerprint_over_unsigned(
        self, archived: GraphConfig
    ) -> None:
        """So nothing downstream has to remember. A band, a Hamming distance
        or a hex rendering taken from a negative value is wrong in a way that
        produces no clusters rather than wrong ones."""
        found = {one.id: one.simhash for one in _facts(archived)}

        assert all(value >= 0 for value in found.values())
        assert all(value < 1 << 64 for value in found.values())

    def test_the_unsigned_value_is_the_signed_one_read_again(
        self, archived: GraphConfig
    ) -> None:
        """The same bits, not a different number."""
        found = {one.id: one.simhash for one in _facts(archived)}

        with client.session(archived) as graph:
            stored = dict(
                graph.execute("MATCH (m:Message) RETURN m.id, m.simhash").rows
            )

        assert {key: to_signed_64(value) for key, value in found.items()} == stored


class TestWhoTheArchiveBelongsTo:
    """The account list, and therefore what "sent by me" is allowed to mean."""

    def test_the_archive_owns_one_address(self, archived: GraphConfig) -> None:
        with client.session(archived) as graph:
            assert read_account_addresses(graph) == frozenset({corpus.OWN})

    def test_the_direction_of_every_message_follows_from_it(
        self, archived: GraphConfig
    ) -> None:
        """A classification only ever as good as the account list: a shared
        mailbox or an alias nobody imported reads as received."""
        outbound = {one.id for one in _facts(archived) if one.outbound}

        assert corpus.canonical("s01") in outbound
        assert corpus.canonical("n01") not in outbound
        assert len(outbound) == 19


class TestTheBccColumns:
    """Two sets out of one read, because two analyses disagree about who counts."""

    def test_a_blind_copy_stays_out_of_the_addressed_set(
        self, archived: GraphConfig
    ) -> None:
        found = {one.id: one for one in _facts(archived)}[corpus.canonical("b1")]

        assert found.addressed == (corpus.ANNA,)
        assert corpus.REVISION not in found.addressed

    def test_it_is_in_the_participant_set_all_the_same(
        self, archived: GraphConfig
    ) -> None:
        """``participant_key`` was hashed over it, so a group whose size was
        counted over anything narrower would disagree with its own key."""
        found = {one.id: one for one in _facts(archived)}[corpus.canonical("b1")]

        assert found.participants == (corpus.ANNA, corpus.OWN, corpus.REVISION)


class TestWhatTheReaderStepsOver:
    """A graph that has been around holds things the writer cannot produce."""

    def test_a_message_without_a_canonical_id_is_skipped_and_counted(
        self, archived: GraphConfig
    ) -> None:
        """Skipped the way ``MessageRepository`` skips one, and for the same
        reason: a rebuild that tripped over it would take the whole job down.

        Counted separately, because the two reads filter it out in Cypher and
        a caller comparing totals would only learn that something was missing.
        """
        with client.session(archived) as graph:
            graph.execute("CREATE (m:Message {subject: 'no canonical id'})")
            skipped = count_unidentified(graph)
            found = read_facts(graph, CONFIG)
            counted = ground_truth(graph)["Message"]

        assert skipped == 1
        assert counted == 34
        assert len(found) == 33

    def test_a_timestamp_that_will_not_parse_costs_one_message_its_date(
        self, archived: GraphConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """And nothing else. Every analysis already handles an undated
        message, so a rebuild that died over one malformed property would be
        strictly worse than one that reports it and carries on.

        Reachable because raw Cypher goes past the converter that wrote the
        value, so what comes back is whatever string is in the graph.
        """
        with client.session(archived) as graph:
            graph.execute(
                "CREATE (m:Message {id: $id, sent_at: 'gestern', simhash: 0, refs: []})",
                {"id": "broken@nordlicht.example"},
            )
            with caplog.at_level("WARNING"):
                found = read_facts(graph, CONFIG)

        broken = next(one for one in found if one.id == "broken@nordlicht.example")
        assert len(found) == 34
        assert broken.sent_at is None
        assert "unparseable timestamp" in caplog.text

    def test_a_timestamp_with_no_zone_is_read_as_utc(
        self, archived: GraphConfig
    ) -> None:
        """A parseable value that carries no offset is the gap between the two
        guards above, and it is the expensive one.

        The archiver forces UTC on a naive ``Date`` header, so nothing the
        writer produces lands here — but a hand-written fixture, a smoke test
        or an older schema can, and a single naive value makes every ``min``,
        ``max`` and subtraction over the archive's dates raise
        ``TypeError: can't compare offset-naive and offset-aware datetimes``.
        That takes the whole rebuild down, where the two neighbouring cases
        cost one message its date.
        """
        with client.session(archived) as graph:
            graph.execute(
                "CREATE (m:Message {id: $id, sent_at: '2026-02-01T08:00:00', "
                "simhash: 0, refs: []})",
                {"id": "naive@nordlicht.example"},
            )
            found = read_facts(graph, CONFIG)

        naive = next(one for one in found if one.id == "naive@nordlicht.example")
        assert naive.sent_at == datetime(2026, 2, 1, 8, 0, tzinfo=UTC)
        assert all(
            one.sent_at is None or one.sent_at.tzinfo is not None for one in found
        )

    def test_a_message_with_no_date_at_all_is_simply_undated(
        self, archived: GraphConfig
    ) -> None:
        """Mail without a ``Date`` header exists, and the analyses all cope:
        it counts towards a pair and a group, and towards a template's
        frequency, while saying nothing about when anything happened."""
        with client.session(archived) as graph:
            graph.execute(
                "CREATE (m:Message {id: $id, simhash: 0, refs: []})",
                {"id": "undated@nordlicht.example"},
            )
            found = read_facts(graph, CONFIG)

        undated = next(one for one in found if one.id == "undated@nordlicht.example")
        assert undated.sent_at is None
        assert undated.participants == ()


class TestTheCeiling:
    """``max_messages`` is a ceiling *and* an ordering."""

    def test_a_capped_read_returns_the_first_messages_by_id(
        self, archived: GraphConfig
    ) -> None:
        capped = _facts(archived, CONFIG.model_copy(update={"max_messages": 5}))

        assert [one.id for one in capped] == [
            corpus.canonical(key) for key in ("b1", "b2", "f1", "f2", "n01")
        ]

    def test_two_capped_reads_return_the_same_messages(
        self, archived: GraphConfig
    ) -> None:
        """Without the ordering this is a coin toss, and two rebuilds would
        cluster different subsets and mint different topic ids."""
        capped = CONFIG.model_copy(update={"max_messages": 7})

        assert _facts(archived, capped) == _facts(archived, capped)

    def test_a_ceiling_of_zero_means_the_whole_archive(
        self, archived: GraphConfig
    ) -> None:
        assert (
            len(_facts(archived, CONFIG.model_copy(update={"max_messages": 0}))) == 33
        )

    def test_a_ceiling_beyond_the_archive_is_harmless(
        self, archived: GraphConfig
    ) -> None:
        """The paged walk stops on a short page rather than on an exact count."""
        assert (
            len(_facts(archived, CONFIG.model_copy(update={"max_messages": 500}))) == 33
        )


class TestTheBatchSizes:
    """The two constants that only do anything on an archive no test builds.

    ``PAGE_SIZE`` is two thousand and ``BODY_BATCH`` five hundred, so against a
    thirty-three-message corpus every loop in this module runs exactly once and
    an edit to the cursor arithmetic reads correctly, passes the whole suite,
    and then loses or repeats messages past row two thousand. Turning the
    constants down is what makes those loops run against a real graph.
    """

    def test_the_paged_walk_reads_every_message_once(
        self, archived: GraphConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seven pages over thirty-three messages: no gap and no repeat."""
        monkeypatch.setattr(reader, "PAGE_SIZE", 5)

        found = _facts(archived)

        assert len(found) == 33
        assert [one.id for one in found] == sorted({one.id for one in found})

    def test_a_multi_page_walk_agrees_with_a_single_page_one(
        self, archived: GraphConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Field for field. A cursor that skipped or repeated a row would
        change the facts, not only their number."""
        whole = _facts(archived)

        monkeypatch.setattr(reader, "PAGE_SIZE", 4)

        assert _facts(archived) == whole

    def test_a_ceiling_that_falls_inside_a_page_still_stops_there(
        self, archived: GraphConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Eleven is not a multiple of four, so the last page is asked for a
        short limit rather than trimmed afterwards."""
        monkeypatch.setattr(reader, "PAGE_SIZE", 4)

        capped = _facts(archived, CONFIG.model_copy(update={"max_messages": 11}))

        assert [one.id for one in capped] == [one.id for one in _facts(archived)][:11]

    def test_the_bodies_come_back_across_several_batches(
        self, archived: GraphConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BODY_BATCH`` bounds the size of one ``$ids`` payload; asking for
        twelve bodies three at a time has to answer with all twelve."""
        wanted = [corpus.canonical(f"s{n:02d}") for n in range(1, 13)]
        monkeypatch.setattr(reader, "BODY_BATCH", 3)

        with client.session(archived) as graph:
            bodies = read_bodies(graph, wanted)

        assert set(bodies) == set(wanted)
