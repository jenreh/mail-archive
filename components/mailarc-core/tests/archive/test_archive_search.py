"""The search statements' shape, and the projection of a search into a page.

`FakeSession` hands back canned rows the way `test_archive_reader.py` drives
the listing, and records every compiled statement — so what is proved here is
the *shape*: which filter emits which clause, that the combined ``WHERE``
lands after a plain ``MATCH`` and never after the ``OPTIONAL MATCH`` (where
Cypher would nullify instead of drop — the one ordering bug this family can
regress into), and that the reader's dispatch, ordering and score scaling do
what their docstrings say. `test_archive_search_local.py` proves the same
statements against a real FalkorDB.

Backticks are stripped from the recorded Cypher, as in the reader tests:
runic quotes every identifier it emits, and that is escaping, not shape.

The date bounds are naive where the ``noqa: DTZ001`` markers say so, because
that is the only shape the application ever builds: the search page's
``parse_date`` strips the offset, so a picked day means the wall-clock day the
rows show. An aware bound would test an input no page can produce.
"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import Address, Label, Message
from mailarc_core.archive.reader import ArchiveReader, GraphSessionFactory
from mailarc_core.archive.repository import AddressRepository, MessageRepository
from mailarc_core.archive.search import (
    ScoredId,
    SearchFilters,
    searchable_terms,
)
from mailarc_core.mail.model import LabelKind

SENT_AT = datetime(2026, 8, 19, 14, 28, tzinfo=UTC)


class FakeSession:
    """A `runic.ogm.Session` stand-in answering the search's four reads.

    ``all_with_edges`` answers the label lookup — told apart by the edge it
    walks — with ``labels`` and every listing (filtered, by-ids) with
    ``rows``. ``all_rows`` answers the full-text statement — told apart by
    the procedure call — with ``scored``, the address reads — told apart by
    their projected column — with ``addresses``, and the count statement with
    ``total``. Every statement is recorded compiled, backticks dropped, with
    its bound parameters beside it.
    """

    def __init__(
        self,
        rows: list[tuple[Message, Address | Label | None]] | None = None,
        labels: list[tuple[Message, Address | Label | None]] | None = None,
        scored: list[dict[str, Any]] | None = None,
        addresses: list[dict[str, Any]] | None = None,
        total: int = 0,
    ) -> None:
        self.rows = rows or []
        self.labels = labels or []
        self.scored = scored or []
        self.addresses = addresses or []
        self.total = total
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any]] = []

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def _record(self, statement: Any) -> str:
        cypher, params = statement.build()
        cypher = cypher.replace("`", "")
        self.statements.append(cypher)
        self.parameters.append(params)
        return cypher

    def all_with_edges(
        self, statement: Any
    ) -> list[tuple[Message, Address | Label | None]]:
        cypher = self._record(statement)
        if "LABELED" in cypher:
            return self.labels
        return self.rows

    def all_rows(self, statement: Any) -> list[dict[str, Any]]:
        cypher = self._record(statement)
        if "queryNodes" in cypher:
            return self.scored
        if "AS address" in cypher:
            return self.addresses
        return [{"total": self.total}]

    def count(self, statement: Any) -> int:
        self._record(statement)
        return self.total


def message(**overrides: Any) -> Message:
    fields: dict[str, Any] = {
        "id": "m1@example.com",
        "subject": "Angebot 1",
        "sent_at": SENT_AT,
        "body_text": "anbei das Angebot",
        "has_attachments": False,
    }
    return Message(**{**fields, **overrides})


def sender(**overrides: Any) -> Address:
    fields: dict[str, Any] = {"id": "anna@example.com", "display_names": ["Anna"]}
    return Address(**{**fields, **overrides})


def label(name: str) -> Label:
    return Label(id=f"7:{name}", name=name, kind=LabelKind.USER)


def repository(session: FakeSession) -> MessageRepository:
    return MessageRepository(cast(Any, session))


def addresses(session: FakeSession) -> AddressRepository:
    return AddressRepository(cast(Any, session))


@pytest.fixture
def blobs(tmp_path) -> BlobStore:
    return BlobStore(ArchiveConfig(store_dir=tmp_path / "blobs"))


def reader(session: FakeSession, blobs: BlobStore) -> ArchiveReader:
    return ArchiveReader(cast(GraphSessionFactory, lambda: session), blobs)


class TestTheFilters:
    def test_an_untouched_form_is_empty(self) -> None:
        filters = SearchFilters()

        assert filters.empty is True
        assert filters.structured is False

    def test_whitespace_is_not_a_filter(self) -> None:
        filters = SearchFilters(
            text="  ", senders=(" ",), recipients=(" ", ""), account_id=" "
        )

        assert filters.empty is True

    def test_text_alone_is_not_structured(self) -> None:
        filters = SearchFilters(text="angebot")

        assert filters.empty is False
        assert filters.structured is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"senders": ("anna",)},
            {"recipients": ("bob",)},
            {"account_id": "7"},
            {"sent_from": SENT_AT},
            {"sent_until": SENT_AT},
            {"has_attachments": True},
            {"has_attachments": False},
        ],
    )
    def test_each_structured_field_counts(self, overrides: dict[str, Any]) -> None:
        """``has_attachments=False`` included — the tri-state's whole point."""
        filters = SearchFilters(**overrides)

        assert filters.structured is True
        assert filters.empty is False


class TestTheAddressesAFormCanOffer:
    def test_senders_walk_the_one_edge_a_message_has(self) -> None:
        session = FakeSession(addresses=[{"address": "anna@example.com"}])

        found = addresses(session).find_senders()

        [cypher] = session.statements
        assert "MATCH (m)-[:SENT_FROM]->(s:Address)" in cypher
        assert found == ["anna@example.com"]

    def test_recipients_walk_to_and_cc_and_never_bcc(self) -> None:
        session = FakeSession(addresses=[{"address": "bob@example.com"}])

        addresses(session).find_recipients()

        [cypher] = session.statements
        assert "MATCH (m)-[:SENT_TO|COPIED_TO]->(r:Address)" in cypher
        assert "BLIND" not in cypher

    def test_the_busiest_address_comes_first_and_ties_break_by_key(self) -> None:
        """The picker shows a bounded number of options before anybody types,
        so the order decides which ones those are; the id tiebreak is what
        stops two equally busy addresses swapping between page loads."""
        session = FakeSession()

        addresses(session).find_senders()

        [cypher] = session.statements
        assert "count(DISTINCT m.id) AS messages" in cypher
        assert "ORDER BY messages DESC, address ASC" in cypher

    def test_an_address_without_a_key_is_not_offered(self) -> None:
        """A node a smoke test or an older schema left behind is not an
        address anybody can filter by."""
        session = FakeSession(addresses=[{"address": None}, {"address": ""}])

        assert addresses(session).find_senders() == []
        assert "s.id IS NOT NULL" in session.statements[0]


class TestSearchableTerms:
    def test_words_survive_umlauts_included(self) -> None:
        assert searchable_terms("Rechnung Müller März") == "Rechnung Müller März"

    def test_operators_are_dropped_not_escaped(self) -> None:
        assert searchable_terms("-@subject:(Angebot|Test)*") == "subject Angebot Test"

    def test_a_query_with_no_words_raises(self) -> None:
        with pytest.raises(ValueError, match="no searchable words"):
            searchable_terms("-()*|")


class TestTheFilteredListing:
    def test_a_sender_filter_shares_the_display_traversal(self) -> None:
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(senders=(" Anna@Example.COM ",))
        )

        [cypher] = session.statements
        assert "MATCH (m)-[:SENT_FROM]->(s:Address)" in cypher
        assert "OPTIONAL MATCH" not in cypher
        assert "s.id IN $p0" in cypher
        assert session.parameters[0]["p0"] == ["anna@example.com"]

    def test_several_senders_are_one_in_and_can_be_nothing_else(self) -> None:
        """A message has exactly one ``SENT_FROM`` edge, so "from all of these"
        has no answer and the pattern cannot fan out — one walk, one ``IN``,
        one row per message however many addresses were picked."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(senders=("Anna@example.com", "carl@firma.de"))
        )

        [cypher] = session.statements
        assert cypher.count("MATCH (m)-[:SENT_FROM]->") == 1
        assert "s.id IN $p0" in cypher
        assert session.parameters[0]["p0"] == ["anna@example.com", "carl@firma.de"]

    def test_a_recipient_filter_walks_to_and_cc_in_one_pattern(self) -> None:
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(recipients=("Bob",)))

        [cypher] = session.statements
        assert "MATCH (m)-[:SENT_TO|COPIED_TO]->(r0:Address)" in cypher
        assert "BLIND" not in cypher
        assert "r0.id = $p0" in cypher
        assert session.parameters[0]["p0"] == "bob"

    def test_every_recipient_gets_a_pattern_of_its_own(self) -> None:
        """ "All" is one walk per address: only a message that reached both
        can bind ``r0`` *and* ``r1``. An ``IN`` over one variable would be
        "any" however many values it held."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(recipients=("Bob", "Dora"), recipients_all=True)
        )

        [cypher] = session.statements
        assert "MATCH (m)-[:SENT_TO|COPIED_TO]->(r0:Address)" in cypher
        assert "MATCH (m)-[:SENT_TO|COPIED_TO]->(r1:Address)" in cypher
        assert "(r0.id = $p0) AND (r1.id = $p1)" in cypher
        assert session.parameters[0] == {"p0": "bob", "p1": "dora"}

    def test_any_of_them_is_one_pattern_and_an_in(self) -> None:
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(recipients=("Bob", "Dora"), recipients_all=False)
        )

        [cypher] = session.statements
        assert cypher.count("MATCH (m)-[:SENT_TO|COPIED_TO]->") == 1
        assert "r.id IN $p0" in cypher
        assert session.parameters[0]["p0"] == ["bob", "dora"]

    def test_a_pick_that_is_only_whitespace_filters_nothing(self) -> None:
        """An empty option cannot reach a pattern — a blank ``r0.id = ""``
        would match nothing and empty the result list."""
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(recipients=(" ", "")))

        assert "SENT_TO" not in session.statements[0]

    def test_the_predicate_never_lands_on_the_optional_match(self) -> None:
        """The load-bearing clause order: display traversal first, filter
        ``MATCH`` after it, predicates last — attached to a clause that drops
        rows. On a trailing ``OPTIONAL MATCH`` Cypher would nullify the
        sender instead, and every filter would silently stop filtering."""
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(recipients=("bob",)))

        [cypher] = session.statements
        display = cypher.index("OPTIONAL MATCH (m)-[:SENT_FROM]->(s:Address)")
        narrows = cypher.index("MATCH (m)-[:SENT_TO|COPIED_TO]->(r0:Address)")
        where = cypher.index("WHERE r0.id =")
        assert display < narrows < where

    def test_an_account_filter_matches_the_key_exactly(self) -> None:
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(account_id="7"))

        [cypher] = session.statements
        assert "MATCH (m)-[:ARCHIVED_FROM]->(a:Account)" in cypher
        assert "a.id = $p0" in cypher
        assert session.parameters[0]["p0"] == "7"

    def test_the_date_range_is_bound_as_naive_iso_strings(self) -> None:
        """An aware bound loses its offset, not its wall-clock — the stored
        strings are wall-clock too, and that margin is documented."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(
                sent_from=datetime(2026, 3, 1, tzinfo=UTC),
                sent_until=datetime(2026, 3, 31, 23, 59, 59),  # noqa: DTZ001
            )
        )

        [cypher] = session.statements
        assert "m.sent_at >= $p0" in cypher
        assert "m.sent_at <= $p1" in cypher
        assert session.parameters[0]["p0"] == "2026-03-01T00:00:00"
        assert session.parameters[0]["p1"] == "2026-03-31T23:59:59"

    @pytest.mark.parametrize("wanted", [True, False])
    def test_the_attachment_tristate_filters_both_ways(self, wanted: bool) -> None:
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(has_attachments=wanted))

        [cypher] = session.statements
        assert "m.has_attachments = $p0" in cypher
        assert session.parameters[0]["p0"] is wanted

    def test_no_attachment_preference_emits_no_clause(self) -> None:
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(recipients=("bob",)))

        assert "has_attachments" not in session.statements[0]

    def test_a_file_type_walks_to_the_attachment_and_matches_it(self) -> None:
        """A plain ``MATCH`` and never an optional one: a message with no
        matching file is what this filter is there to exclude."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(attachment_types=("application/pdf", "image/png"))
        )

        [cypher] = session.statements
        assert "HAS_ATTACHMENT" in cypher
        assert "OPTIONAL MATCH (m)-[:HAS_ATTACHMENT]" not in cypher
        assert "f.content_type IN $p0" in cypher
        assert session.parameters[0]["p0"] == ["application/pdf", "image/png"]

    def test_a_file_pick_never_counts_a_cid_logo(self) -> None:
        """The edge is bound and read, not merely walked: a part the HTML
        pulls in by ``cid:`` and the sender never named is a signature
        picture, and ``Message.has_attachments`` already counts it out. The
        picker cannot offer one, and this is why asking for one anyway still
        finds nothing."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(attachment_types=("image/png",))
        )

        [cypher] = session.statements
        assert "(m)-[h:HAS_ATTACHMENT]->(f:Attachment)" in cypher
        assert "(h.filename IS NOT NULL) OR (h.content_id IS NULL)" in cypher

    def test_a_file_name_matches_the_edge_that_carries_it(self) -> None:
        """The half of the pick that finds the invoices nobody typed. The name
        hangs on the edge — the node is content-addressed — so this is the one
        filter that reads a property of the relationship."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(attachment_names=("Rechnung.pdf",))
        )

        [cypher] = session.statements
        assert "h.filename IN $p0" in cypher
        assert "f.content_type IN" not in cypher
        assert session.parameters[0]["p0"] == ["Rechnung.pdf"]

    def test_a_type_and_a_name_are_ored_and_not_anded(self) -> None:
        """One family expands into both halves — the types this archive states
        for it and the names of the shrugs that belong to it — so a message
        matches when either says so. ANDed it would ask for a file that is
        both, which nothing is."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(
                attachment_types=("application/pdf",),
                attachment_names=("Rechnung.pdf",),
            )
        )

        [cypher] = session.statements
        assert "(f.content_type IN $p0) OR (h.filename IN $p1)" in cypher

    def test_no_file_type_walks_no_attachment_edge(self) -> None:
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(recipients=("bob",)))

        assert "HAS_ATTACHMENT" not in session.statements[0]

    def test_no_file_name_walks_no_attachment_edge_either(self) -> None:
        """Both halves empty is no filter at all — an empty ``IN`` matches
        nothing in Cypher, so a clause emitted for an unset pick would empty
        the page."""
        session = FakeSession()

        repository(session).find_filtered(SearchFilters(attachment_names=("", " ")))

        assert "HAS_ATTACHMENT" not in session.statements[0]

    def test_a_file_type_gets_the_with_stage_the_backend_demands(self) -> None:
        """The listing's sender walk is an ``OPTIONAL MATCH`` when no sender
        was picked, and a plain ``MATCH`` may not follow one without a ``WITH``
        between — measured against FalkorDB, which refuses the statement
        outright rather than answering it wrongly."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(attachment_types=("application/pdf",))
        )

        [cypher] = session.statements
        assert cypher.index("WITH") < cypher.index("HAS_ATTACHMENT")

    def test_every_filter_at_once_keeps_the_listing_shape(self) -> None:
        """Distinct before the page cut, newest first with the id tiebreak,
        sender in the returned pair — the recent listing, narrowed."""
        session = FakeSession()

        repository(session).find_filtered(
            SearchFilters(
                senders=("anna",),
                recipients=("bob",),
                account_id="7",
                sent_from=datetime(2026, 3, 1),  # noqa: DTZ001
                sent_until=datetime(2026, 3, 31),  # noqa: DTZ001
                has_attachments=True,
                attachment_types=("application/pdf",),
            ),
            limit=10,
            offset=20,
        )

        [cypher] = session.statements
        assert "m.id IS NOT NULL" in cypher
        assert "RETURN DISTINCT m, s" in cypher
        assert "ORDER BY m.sent_at DESC, m.id ASC" in cypher
        assert "SKIP 20" in cypher
        assert "LIMIT 10" in cypher


class TestTheFilteredCount:
    def test_the_count_is_over_distinct_messages(self) -> None:
        """The recipient alternation fans out; the total counts messages."""
        session = FakeSession(total=3)

        total = repository(session).count_filtered(SearchFilters(recipients=("bob",)))

        assert total == 3
        [cypher] = session.statements
        assert "count(DISTINCT m.id) AS total" in cypher
        assert "MATCH (m)-[:SENT_TO|COPIED_TO]->(r0:Address)" in cypher
        assert "SKIP" not in cypher
        assert "LIMIT" not in cypher

    def test_a_count_asks_for_no_display_sender(self) -> None:
        session = FakeSession()

        repository(session).count_filtered(SearchFilters(recipients=("bob",)))

        assert "OPTIONAL MATCH" not in session.statements[0]


class TestTheFulltext:
    def test_the_proven_shape_procedure_then_where_then_score_order(self) -> None:
        session = FakeSession()

        repository(session).search_fulltext(
            SearchFilters(text="Angebot März"), limit=5, offset=10
        )

        [cypher] = session.statements
        call = cypher.index("CALL db.idx.fulltext.queryNodes('Message', $__fts_query)")
        yielded = cypher.index("WITH m, score AS __score")
        guard = cypher.index("m.id IS NOT NULL")
        assert call < yielded < guard
        assert "RETURN DISTINCT m.id AS id, __score AS relevance" in cypher
        assert "ORDER BY relevance DESC, id ASC" in cypher
        assert "SKIP 10" in cypher
        assert "LIMIT 5" in cypher
        assert session.parameters[0]["__fts_query"] == "Angebot März"

    def test_structured_filters_apply_after_the_yield(self) -> None:
        """The index cannot be narrowed before the fact; the account filter
        has to land on what the procedure produced."""
        session = FakeSession()

        repository(session).search_fulltext(
            SearchFilters(text="angebot", account_id="7")
        )

        [cypher] = session.statements
        yielded = cypher.index("WITH m, score AS __score")
        narrows = cypher.index("MATCH (m)-[:ARCHIVED_FROM]->(a:Account)")
        where = cypher.index("WHERE a.id = $p1")
        assert yielded < narrows < where

    def test_the_query_text_is_sanitised_before_the_index_sees_it(self) -> None:
        session = FakeSession()

        repository(session).search_fulltext(SearchFilters(text="-@subject:(Angebot)*"))

        assert session.parameters[0]["__fts_query"] == "subject Angebot"

    def test_an_operator_only_query_raises_instead_of_searching(self) -> None:
        session = FakeSession()

        with pytest.raises(ValueError, match="no searchable words"):
            repository(session).search_fulltext(SearchFilters(text="-()*"))

        assert session.statements == []

    def test_rows_come_back_as_scored_ids_in_index_order(self) -> None:
        session = FakeSession(
            scored=[
                {"id": "m2", "relevance": 8.0},
                {"id": "m1", "relevance": 2.0},
            ]
        )

        scored = repository(session).search_fulltext(SearchFilters(text="angebot"))

        assert scored == [
            ScoredId(id="m2", relevance=8.0),
            ScoredId(id="m1", relevance=2.0),
        ]


class TestFindByIds:
    def test_the_callers_order_is_the_answer_order(self) -> None:
        first, second = message(id="a"), message(id="b")
        session = FakeSession(rows=[(first, sender()), (second, None)])

        rows = repository(session).find_by_ids(["b", "a"])

        assert [one.id for one, _ in rows] == ["b", "a"]
        [cypher] = session.statements
        assert "WHERE m.id IN $p0" in cypher
        assert "OPTIONAL MATCH (m)-[:SENT_FROM]->(s:Address)" in cypher

    def test_an_id_the_graph_no_longer_holds_is_left_out(self) -> None:
        session = FakeSession(rows=[(message(id="a"), None)])

        rows = repository(session).find_by_ids(["gone", "a"])

        assert [one.id for one, _ in rows] == ["a"]

    def test_empty_input_never_reaches_the_graph(self) -> None:
        session = FakeSession()

        assert repository(session).find_by_ids([]) == []
        assert session.statements == []


class TestSearchMessages:
    def test_an_empty_form_is_the_recent_listing_with_its_count(self, blobs) -> None:
        session = FakeSession(rows=[(message(), sender())], total=41)

        page = reader(session, blobs).search_messages(SearchFilters())

        assert page.total == 41
        [hit] = page.hits
        assert hit.relevance is None
        assert hit.summary.subject == "Angebot 1"
        listing = session.statements[0]
        assert "OPTIONAL MATCH (m)-[:SENT_FROM]->(s:Address)" in listing
        assert "CONTAINS" not in listing
        assert "queryNodes" not in listing

    def test_a_structured_search_counts_but_does_not_rank(self, blobs) -> None:
        session = FakeSession(
            rows=[(message(), sender())],
            labels=[(message(), label("Kunden"))],
            total=7,
        )

        page = reader(session, blobs).search_messages(SearchFilters(senders=("anna",)))

        assert page.total == 7
        [hit] = page.hits
        assert hit.relevance is None
        assert [one.name for one in hit.summary.labels] == ["Kunden"]
        assert any("count(DISTINCT m.id)" in one for one in session.statements)

    def test_a_text_search_ranks_against_its_best_hit(self, blobs) -> None:
        """Raw 8 and 2 become 1.0 and 0.25 — a ranking within one answer —
        and the hits keep the index's order however the hydration returns."""
        best, other = message(id="m2", subject="Zwei"), message(id="m1")
        session = FakeSession(
            rows=[(other, sender()), (best, sender())],
            scored=[
                {"id": "m2", "relevance": 8.0},
                {"id": "m1", "relevance": 2.0},
            ],
        )

        page = reader(session, blobs).search_messages(SearchFilters(text="angebot"))

        assert page.total is None
        assert [one.summary.id for one in page.hits] == ["m2", "m1"]
        assert [one.relevance for one in page.hits] == [1.0, 0.25]

    def test_all_zero_scores_stay_zero(self, blobs) -> None:
        session = FakeSession(
            rows=[(message(), sender())],
            scored=[{"id": "m1@example.com", "relevance": 0.0}],
        )

        page = reader(session, blobs).search_messages(SearchFilters(text="angebot"))

        assert [one.relevance for one in page.hits] == [0.0]

    def test_a_hit_without_a_row_is_dropped_not_rendered_empty(self, blobs) -> None:
        session = FakeSession(
            rows=[(message(id="m1"), None)],
            scored=[
                {"id": "vanished", "relevance": 9.0},
                {"id": "m1", "relevance": 3.0},
            ],
        )

        page = reader(session, blobs).search_messages(SearchFilters(text="angebot"))

        assert [one.summary.id for one in page.hits] == ["m1"]

    def test_text_hits_carry_their_labels(self, blobs) -> None:
        session = FakeSession(
            rows=[(message(), sender())],
            labels=[(message(), label("Projekte"))],
            scored=[{"id": "m1@example.com", "relevance": 1.0}],
        )

        page = reader(session, blobs).search_messages(SearchFilters(text="angebot"))

        [hit] = page.hits
        assert [one.name for one in hit.summary.labels] == ["Projekte"]


class TestMessagesByIds:
    def test_summaries_come_back_in_the_asked_order_with_labels(self, blobs) -> None:
        first, second = message(id="a", subject="A"), message(id="b", subject="B")
        session = FakeSession(
            rows=[(first, sender()), (second, None)],
            labels=[(second, label("Rechnungen"))],
        )

        summaries = reader(session, blobs).messages_by_ids(["b", "a"])

        assert [one.id for one in summaries] == ["b", "a"]
        assert [one.name for one in summaries[0].labels] == ["Rechnungen"]
        assert summaries[1].sender_address == "anna@example.com"

    def test_no_ids_opens_no_session(self, blobs) -> None:
        session = FakeSession()

        assert reader(session, blobs).messages_by_ids([]) == []
        assert session.statements == []
