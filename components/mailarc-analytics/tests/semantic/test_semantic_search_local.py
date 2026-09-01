"""Both search paths against a real FalkorDB, including the failure it hides.

A vector store is not something a stub can stand in for, and one of its
behaviours is the reason this file exists at all: **FalkorDB accepts a vector
of the wrong length, stores it, and silently declines to index it.** No
exception, no log line, no ``indexingFailures``. The test that writes a
short vector by hand and then cannot find it is the measurement behind every
guard in this package — without it, the guards look like defensive noise.

The rest is what only a server can answer: that the KNN procedure really does
join to later patterns, that its score is a distance and can come back very
slightly negative, that a message with no vector is *absent* rather than ranked
last, and that RediSearch reads the words it is given as an AND.
"""

from typing import Any

import pytest
from semantic_stubs import StubEmbedder

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import rows_of
from mailarc_analytics.semantic.config import SemanticConfig
from mailarc_analytics.semantic.errors import (
    NO_FULLTEXT_INDEX,
    NO_VECTOR_INDEX,
    SearchQueryError,
    SemanticUnavailable,
)
from mailarc_analytics.semantic.model import SearchKind, SearchRequest
from mailarc_analytics.semantic.search import (
    SemanticSearch,
    coverage,
    fulltext_hits,
    has_fulltext_index,
    knn_hits,
    similar_pairs,
    vector_index,
)
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

pytestmark = pytest.mark.graph_local

MODEL = "stub-model"
DIMENSION = 4

INVOICE = [1.0, 0.0, 0.0, 0.0]
NEARLY_INVOICE = [0.99, 0.14, 0.0, 0.0]
LUNCH = [0.0, 1.0, 0.0, 0.0]

PLANTED: list[dict[str, Any]] = [
    {
        "id": "m-invoice",
        "subject": "Rechnung Q3",
        "body": "Die Rechnung fuer das dritte Quartal liegt bei.",
        "sent_at": "2026-01-12T09:00:00+00:00",
        "vector": INVOICE,
    },
    {
        "id": "m-reminder",
        "subject": "Erinnerung Rechnung",
        "body": "Die Rechnung ist noch offen.",
        "sent_at": "2026-02-03T07:45:00+00:00",
        "vector": NEARLY_INVOICE,
    },
    {
        "id": "m-lunch",
        "subject": "Mittagessen",
        "body": "Ramen um zwoelf?",
        "sent_at": "2026-03-01T12:00:00+00:00",
        "vector": LUNCH,
    },
]


def plant(
    config: GraphConfig, rows: list[dict[str, Any]], *, model: str = MODEL
) -> None:
    """Write messages and, where a row has one, their vector.

    The vectors go in through the catalogue's own write statement rather than
    through hand-written Cypher: it is the statement the embed job uses, so a
    search test is reading what a job would really have written.
    """
    with client.session(config) as graph:
        graph.execute(
            "UNWIND $rows AS row CREATE (m:Message {id: row.id, subject: row.subject, "
            "body_text: row.body, body_clean: row.body, sent_at: row.sent_at})",
            {"rows": [{k: v for k, v in row.items() if k != "vector"} for row in rows]},
        )
        graph.execute(
            "MATCH (m:Message {id: 'm-invoice'}) MERGE (a:Address {id: 'anna@kunde.example'}) "
            "MERGE (m)-[:SENT_FROM]->(a)",
            {},
        )
        embedded = [row for row in rows if row.get("vector")]
        if embedded:
            rows_of(
                graph,
                catalog.WRITE_EMBEDDINGS,
                {
                    "rows": [
                        {"id": row["id"], "vector": row["vector"]} for row in embedded
                    ],
                    "model": model,
                },
            )


def searcher(config: GraphConfig, embedder: StubEmbedder | None) -> SemanticSearch:
    """A search reading the graph this test planted."""
    return SemanticSearch(
        lambda: client.session(config),
        SemanticConfig(dimension=DIMENSION, knn_over_fetch=4),
        embedder,
    )


class TestTheNeighbourSearch:
    def test_it_ranks_by_similarity_and_prints_a_row(
        self, migrated: GraphConfig
    ) -> None:
        """One query, three messages, and the ordering the archive should
        agree with: the invoice, then the reminder, then lunch."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            hits = knn_hits(graph, INVOICE, k=10, limit=3, model=MODEL)

        assert [one.message_id for one in hits] == [
            "m-invoice",
            "m-reminder",
            "m-lunch",
        ]
        assert hits[0].subject == "Rechnung Q3"
        assert hits[0].sender == "anna@kunde.example"
        assert hits[0].sent_at is not None
        assert hits[0].sent_at.tzinfo is not None

    def test_an_exact_match_never_scores_above_one(self, migrated: GraphConfig) -> None:
        """Measured: an identical normalised vector comes back at a distance of
        ``-1.19e-07``, which turns into a similarity of 100.00001 % on a page
        unless it is clamped."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            hits = knn_hits(graph, INVOICE, k=10, limit=1, model=MODEL)

        assert 0.0 <= hits[0].score <= 1.0
        assert hits[0].score == pytest.approx(1.0, abs=1e-4)

    def test_a_message_with_no_vector_is_absent_rather_than_last(
        self, migrated: GraphConfig
    ) -> None:
        """The heart of the coverage problem: an un-embedded message is not in
        the index at all, so a KNN over a half-embedded archive returns a
        short result set that looks exactly like a complete one."""
        plant(
            migrated,
            [
                *PLANTED,
                {
                    "id": "m-none",
                    "subject": "no vector",
                    "body": "nothing",
                    "sent_at": None,
                },
            ],
        )

        with client.session(migrated) as graph:
            hits = knn_hits(graph, INVOICE, k=10, limit=10, model=MODEL)
            found = coverage(graph, MODEL)

        assert "m-none" not in [one.message_id for one in hits]
        assert (found.total, found.embedded) == (4, 3)
        assert "1 of 4" in found.describe()

    def test_a_message_without_a_canonical_id_is_skipped(
        self, migrated: GraphConfig
    ) -> None:
        """The same filter every other read in this project applies: the
        writer cannot produce such a node, but a graph that has been around
        can hold one."""
        plant(
            migrated,
            [
                *PLANTED,
                {
                    "id": "",
                    "subject": "kein id",
                    "body": "x",
                    "sent_at": None,
                    "vector": INVOICE,
                },
            ],
        )

        with client.session(migrated) as graph:
            hits = knn_hits(graph, INVOICE, k=10, limit=10, model=MODEL)

        assert "" not in [one.message_id for one in hits]

    def test_k_bounds_the_search_and_limit_bounds_the_answer(
        self, migrated: GraphConfig
    ) -> None:
        """The two numbers are not the same number. ``k`` is how wide the
        index search goes; anything filtered afterwards has to have been
        over-fetched, because the procedure cannot be narrowed beforehand."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            narrow = knn_hits(graph, INVOICE, k=1, limit=10, model=MODEL)
            wide = knn_hits(graph, INVOICE, k=10, limit=2, model=MODEL)

        assert len(narrow) == 1
        assert len(wide) == 2


class TestTheTrapThatHides:
    def test_a_vector_of_the_wrong_length_is_stored_and_never_found(
        self, migrated: GraphConfig
    ) -> None:
        """The measurement every guard in this package exists for.

        The write succeeds. The property is there. The index ignores it, with
        no exception, no log line and no failure count — so a job run against
        a mismatched index would report every message embedded and no search
        would ever return one. Nothing on the server side will catch this,
        which is why :meth:`EmbeddingBatch.assemble` and
        :func:`~mailarc_analytics.semantic.indexing.verify` catch it here.
        """
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            graph.execute(
                "MATCH (m:Message {id: 'm-lunch'}) "
                "SET m.embedding = vecf32($vector), m.embedding_model = $model",
                {"vector": [1.0, 0.0], "model": MODEL},
            )
            stored = graph.execute(
                "MATCH (m:Message {id: 'm-lunch'}) "
                "RETURN typeOf(m.embedding), m.embedding",
                {},
            )
            hits = knn_hits(graph, LUNCH, k=10, limit=10, model=MODEL)

        kind, rendered = stored.rows[0]
        assert kind == "Vectorf32"
        assert str(rendered).count(",") == 1, "the graph really did store two floats"
        assert "m-lunch" not in [one.message_id for one in hits]


class TestTheFullTextSearch:
    def test_it_finds_the_words_it_was_given(self, migrated: GraphConfig) -> None:
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            hits = fulltext_hits(graph, "Rechnung", limit=10)

        assert {one.message_id for one in hits} == {"m-invoice", "m-reminder"}

    def test_two_words_narrow_rather_than_widen(self, migrated: GraphConfig) -> None:
        """RediSearch reads a space as AND, which is the useful default for an
        archive: a caller who wants either can ask twice."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            hits = fulltext_hits(graph, "Rechnung Quartal", limit=10)

        assert [one.message_id for one in hits] == ["m-invoice"]

    def test_an_operator_cannot_invert_the_search(self, migrated: GraphConfig) -> None:
        """``-Rechnung`` is a negation in RediSearch's language. Sent through
        as typed it would return everything *except* the invoices; tokenised,
        it is a search for the word."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            hits = fulltext_hits(graph, "-Rechnung", limit=10)

        assert {one.message_id for one in hits} == {"m-invoice", "m-reminder"}

    def test_a_query_that_would_not_parse_never_reaches_the_parser(
        self, migrated: GraphConfig
    ) -> None:
        """A lone bracket is a syntax error at the store. The caller gets a
        sentence about their words instead of a failed search."""
        plant(migrated, PLANTED)

        with (
            client.session(migrated) as graph,
            pytest.raises(SearchQueryError, match="searchable words"),
        ):
            fulltext_hits(graph, "(((", limit=10)

    def test_the_score_is_relative_to_the_best_hit(self, migrated: GraphConfig) -> None:
        """RediSearch's relevance is unbounded and means nothing across two
        queries, so it is scaled to this answer and labelled as such."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            hits = fulltext_hits(graph, "Rechnung", limit=10)

        assert max(one.score for one in hits) == pytest.approx(1.0)
        assert all(0.0 <= one.score <= 1.0 for one in hits)


class TestWhatTheSchemaDecides:
    def test_the_live_index_reports_its_dimension(self, migrated: GraphConfig) -> None:
        """Read before a job writes anything, because the configuration is
        exactly what can be wrong."""
        with client.session(migrated) as graph:
            index = vector_index(graph)

        assert index is not None
        assert (index.label, index.prop) == ("Message", "embedding")
        assert (index.dimension, index.similarity) == (DIMENSION, "cosine")

    def test_an_unmigrated_graph_has_neither_index(
        self, unmigrated: GraphConfig
    ) -> None:
        with client.session(unmigrated) as graph:
            assert vector_index(graph) is None
            assert not has_fulltext_index(graph)

    async def test_a_semantic_search_without_the_index_says_which_command(
        self, unmigrated: GraphConfig
    ) -> None:
        """The embedder is fine and the schema is behind — a different fix
        from the embedder-off case, so a different message."""
        plant(unmigrated, [{"id": "m", "subject": "s", "body": "b", "sent_at": None}])

        with pytest.raises(SemanticUnavailable) as caught:
            await searcher(unmigrated, StubEmbedder(dimension=DIMENSION)).semantic(
                SearchRequest(text="invoice", kind=SearchKind.SEMANTIC)
            )

        assert str(caught.value) == NO_VECTOR_INDEX

    def test_a_full_text_search_without_the_index_says_so_too(
        self, unmigrated: GraphConfig
    ) -> None:
        """This one takes down the path that is supposed to work with nothing
        configured, so it gets its own sentence."""
        plant(unmigrated, [{"id": "m", "subject": "s", "body": "b", "sent_at": None}])

        with (
            client.session(unmigrated) as graph,
            pytest.raises(SemanticUnavailable) as caught,
        ):
            fulltext_hits(graph, "anything", limit=5)

        assert str(caught.value) == NO_FULLTEXT_INDEX

    async def test_a_query_vector_of_the_wrong_length_is_refused_by_name(
        self, migrated: GraphConfig
    ) -> None:
        """The store would refuse it too, with ``Vector dimension mismatch`` —
        true, opaque, and naming neither the model nor the setting."""
        plant(migrated, PLANTED)

        with pytest.raises(SemanticUnavailable, match="app_semantic_dimension"):
            await searcher(migrated, StubEmbedder(dimension=3)).semantic(
                SearchRequest(text="invoice", kind=SearchKind.SEMANTIC)
            )


class TestTheFacadeEndToEnd:
    async def test_a_semantic_search_answers_hits_and_coverage(
        self, migrated: GraphConfig
    ) -> None:
        """The whole path: embed the query, search the index, report how much
        of the archive the answer could even see."""
        embedder = StubEmbedder(dimension=DIMENSION)
        plant(
            migrated,
            [
                {
                    "id": "m-a",
                    "subject": "a",
                    "body": "a",
                    "sent_at": None,
                    "vector": list(embedder.vector_for("a\n\na")),
                },
                {"id": "m-b", "subject": "b", "body": "bbbbbbbbbb", "sent_at": None},
            ],
        )

        result = await searcher(migrated, embedder).semantic(
            SearchRequest(text="a\n\na", kind=SearchKind.SEMANTIC)
        )

        assert result.kind is SearchKind.SEMANTIC
        assert [one.message_id for one in result.hits] == ["m-a"]
        assert result.coverage is not None
        assert "1 of 2" in result.notice

    async def test_full_text_works_with_no_embedder_at_all(
        self, migrated: GraphConfig
    ) -> None:
        """The claim the no-embedder message makes, checked rather than
        asserted in prose: everything except the vector path keeps working."""
        plant(migrated, PLANTED)

        result = await searcher(migrated, None).search(SearchRequest(text="Rechnung"))

        assert result.kind is SearchKind.FULLTEXT
        assert {one.message_id for one in result.hits} == {"m-invoice", "m-reminder"}
        assert result.notice == ""

    def test_coverage_reads_as_nothing_embedded_with_no_model(
        self, migrated: GraphConfig
    ) -> None:
        """What a page shows beside a disabled search box: honest, and not an
        error."""
        plant(migrated, PLANTED)

        found = searcher(migrated, None).coverage()

        assert (found.total, found.embedded) == (3, 0)


class TestTheEmbedderSwitchTrap:
    """§7.4's stated hazard, measured: changing embedder must degrade to
    *fewer* hits, never to hits scored in the model that was replaced."""

    async def test_a_vector_from_the_previous_model_is_never_ranked(
        self, migrated: GraphConfig
    ) -> None:
        """The one KNN in the index holds vectors from two models at once,
        which is what a half-finished re-embed leaves behind. Only the model
        the query was embedded with may answer, or the ranking is a comparison
        between two spaces and the coverage notice beside it is a lie.
        """
        plant(migrated, PLANTED, model="model-a")
        with client.session(migrated) as graph:
            rows_of(
                graph,
                catalog.WRITE_EMBEDDINGS,
                {"rows": [{"id": "m-lunch", "vector": LUNCH}], "model": "model-b"},
            )

        result = await searcher(
            migrated, StubEmbedder(model="model-b", dimension=DIMENSION)
        ).semantic(SearchRequest(text="Rechnung", kind=SearchKind.SEMANTIC, limit=10))

        assert [one.message_id for one in result.hits] == ["m-lunch"]
        assert result.coverage is not None
        assert (result.coverage.embedded, result.coverage.total) == (1, 3)

    def test_the_neighbour_read_takes_the_model_it_searches_under(
        self, migrated: GraphConfig
    ) -> None:
        """The same rule one level down, where the statement is."""
        plant(migrated, PLANTED, model="model-a")

        with client.session(migrated) as graph:
            same = knn_hits(graph, INVOICE, k=10, limit=10, model="model-a")
            other = knn_hits(graph, INVOICE, k=10, limit=10, model="model-b")

        assert len(same) == 3
        assert other == ()


class TestAnIndexThatHoldsNothing:
    async def test_a_search_before_the_embed_job_names_the_job(
        self, migrated: GraphConfig
    ) -> None:
        """The configuration every installation passes through between setting
        an embedder and finishing the job. An empty list here reads as "your
        archive holds nothing about this", which is the one outcome §10's
        definition of done forbids."""
        plant(migrated, [{"id": "m", "subject": "s", "body": "b", "sent_at": None}])

        with pytest.raises(SemanticUnavailable, match="embed job"):
            await searcher(migrated, StubEmbedder(dimension=DIMENSION)).semantic(
                SearchRequest(text="Rechnung", kind=SearchKind.SEMANTIC)
            )

    async def test_an_empty_archive_is_still_an_ordinary_empty_answer(
        self, migrated: GraphConfig
    ) -> None:
        """Nothing imported is not a misconfiguration, and there is no job to
        recommend — so this one really is an empty result."""
        result = await searcher(migrated, StubEmbedder(dimension=DIMENSION)).semantic(
            SearchRequest(text="Rechnung", kind=SearchKind.SEMANTIC)
        )

        assert result.hits == ()


class TestWhatTheJobCanStillFix:
    def test_a_message_with_no_body_is_not_counted_as_missing(
        self, migrated: GraphConfig
    ) -> None:
        """An attachment-only mail and a fully-quoted reply both leave
        ``body_clean`` empty, and the embed statements exclude them by design.
        Counting them as missing leaves a "run the embed job" notice on every
        answer forever, after the job has run to completion with nothing left
        to do — which teaches the reader to ignore the notice that matters."""
        plant(
            migrated,
            [
                *PLANTED,
                {"id": "m-empty", "subject": "Anhang", "body": "", "sent_at": None},
            ],
        )

        with client.session(migrated) as graph:
            found = coverage(graph, MODEL)

        assert (found.total, found.embedded, found.unembeddable) == (4, 3, 1)
        assert found.missing == 0
        assert found.complete
        assert found.describe() == ""


class TestSignalSixsNeighbours:
    """A2's sixth signal, read in one statement rather than one per message.

    Measured on the vendored FalkorDB: ``db.idx.vector.queryNodes`` accepts the
    vector straight off a matched node, and a ``WHERE`` after its ``YIELD``
    really does narrow what it produced. So the whole archive's neighbours come
    back in one round trip instead of a hundred thousand of them.
    """

    def test_it_pairs_the_messages_that_land_close_together(
        self, migrated: GraphConfig
    ) -> None:
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            pairs = similar_pairs(
                graph, model=MODEL, neighbours=3, minimum=0.5, limit=100
            )

        assert [(one.left, one.right) for one in pairs] == [("m-invoice", "m-reminder")]
        assert pairs[0].score == pytest.approx(0.99, abs=0.01)

    def test_a_message_is_never_its_own_neighbour(self, migrated: GraphConfig) -> None:
        """FalkorDB's KNN returns the query node first for *every* message, so
        without this the archive offers one wasted pair per message — and each
        one spends a slot of the weak-pair budget."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            pairs = similar_pairs(
                graph, model=MODEL, neighbours=3, minimum=0.0, limit=100
            )

        assert all(one.left != one.right for one in pairs)

    def test_a_pair_is_offered_once_and_not_in_both_directions(
        self, migrated: GraphConfig
    ) -> None:
        """The KNN is symmetric: *a* names *b* and *b* names *a*. Offering
        both would spend two slots of the budget on one edge."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            pairs = similar_pairs(
                graph, model=MODEL, neighbours=3, minimum=0.0, limit=100
            )

        assert len(pairs) == len({frozenset((one.left, one.right)) for one in pairs})
        assert all(one.left < one.right for one in pairs)

    def test_the_floor_is_applied_in_the_store(self, migrated: GraphConfig) -> None:
        """0.82 is high on purpose — at 0.7 an invoice and a delivery note are
        neighbours in every model — and paying for the rows only to drop them
        in Python would move the whole archive's cross product over the wire.
        """
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            loose = similar_pairs(
                graph, model=MODEL, neighbours=3, minimum=0.0, limit=100
            )
            strict = similar_pairs(
                graph, model=MODEL, neighbours=3, minimum=0.99, limit=100
            )

        assert len(loose) > len(strict)
        assert all(one.score >= 0.99 for one in strict)

    def test_a_vector_from_another_model_is_not_a_neighbour(
        self, migrated: GraphConfig
    ) -> None:
        """The same embedder-switch rule the search follows: two spaces in one
        index would join two messages on a comparison nobody computed."""
        plant(migrated, PLANTED, model="model-a")
        with client.session(migrated) as graph:
            rows_of(
                graph,
                catalog.WRITE_EMBEDDINGS,
                {"rows": [{"id": "m-lunch", "vector": LUNCH}], "model": "model-b"},
            )
            pairs = similar_pairs(
                graph, model="model-b", neighbours=3, minimum=0.0, limit=100
            )

        assert pairs == ()

    def test_an_archive_with_no_vectors_offers_nothing(
        self, migrated: GraphConfig
    ) -> None:
        """Unlike a search, this is a legitimate empty answer: a rebuild with
        no embedder configured runs on five signals and says nothing about it.
        """
        plant(migrated, [{"id": "m", "subject": "s", "body": "b", "sent_at": None}])

        with client.session(migrated) as graph:
            assert (
                similar_pairs(graph, model=MODEL, neighbours=3, minimum=0.0, limit=100)
                == ()
            )

    def test_the_limit_takes_the_closest_pairs_first(
        self, migrated: GraphConfig
    ) -> None:
        """The ceiling is a real one — a KNN over a hundred thousand messages
        offers a million rows — so what it keeps has to be the best of them."""
        plant(migrated, PLANTED)

        with client.session(migrated) as graph:
            capped = similar_pairs(
                graph, model=MODEL, neighbours=3, minimum=0.0, limit=1
            )
            everything = similar_pairs(
                graph, model=MODEL, neighbours=3, minimum=0.0, limit=100
            )

        assert len(capped) == 1
        assert capped[0].score == max(one.score for one in everything)
