"""The embed job against a real graph, from what it selects to what it wrote.

The scripted tests next door prove the loop's decisions; this file proves the
selection, which is where the real subtleties are and none of them is visible
without a store. A message with no ``body_clean`` must not be pending — there
is nothing to embed, and counting it would have the job report failures nobody
can ever fix. A message under a *different* model must be pending, because that
is what ``Message.embedding_model`` was put on the node for. And a second run
over an unchanged archive must find nothing at all, which is the property that
makes this job re-runnable after a crash.

The corpus is the planted one, archived by the real
:class:`~mailarc_core.archive.writer.MessageArchiver`, for the reason the
component's other local tests give: the point of a round trip is that both ends
are the real thing. The writer deliberately leaves ``embedding`` empty, so what
this job finds is exactly what the import left behind.
"""

from pathlib import Path
from typing import Any

import pytest
from semantic_stubs import StubEmbedder

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import rows_of
from mailarc_analytics.semantic.config import SemanticConfig
from mailarc_analytics.semantic.indexing import (
    INDEX_EF_CONSTRUCTION,
    INDEX_EF_RUNTIME,
    INDEX_M,
    INDEX_SIMILARITY,
    count_pending,
    embed_pending,
    read_pending,
    rebuild_index,
)
from mailarc_analytics.semantic.model import EmbedRun, SearchKind, SearchRequest
from mailarc_analytics.semantic.search import SemanticSearch, coverage, vector_index
from mailarc_core.archive.reader import GraphSessionFactory
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.errors import MailPermanentError

pytestmark = pytest.mark.graph_local

DIMENSION = 4
MODEL = "stub-model"
OTHER_MODEL = "some-other-model"

MESSAGES: list[dict[str, Any]] = [
    {"id": "m-1", "subject": "Rechnung Q3", "body": "Die Rechnung liegt bei."},
    {"id": "m-2", "subject": "Erinnerung", "body": "Die Rechnung ist offen."},
    {"id": "m-3", "subject": "Mittagessen", "body": "Ramen um zwoelf?"},
]


def plant(config: GraphConfig, rows: list[dict[str, Any]]) -> None:
    """Messages as the import would have left them: text, and no vector."""
    with client.session(config) as graph:
        graph.execute(
            "UNWIND $rows AS row CREATE (m:Message {id: row.id, subject: row.subject, "
            "body_text: row.body, body_clean: row.body})",
            {"rows": rows},
        )


def sessions(config: GraphConfig) -> GraphSessionFactory:
    """A session factory over this test's graph."""
    return lambda: client.session(config)


def settings(**overrides: Any) -> SemanticConfig:
    """A configuration sized for a three-message archive."""
    base: dict[str, Any] = {
        "dimension": DIMENSION,
        "page_size": 2,
        "batch_size": 2,
        "max_body_chars": 200,
    }
    return SemanticConfig(**(base | overrides))


def stored_models(config: GraphConfig) -> dict[str, str | None]:
    """Which model each message says it was embedded by."""
    with client.session(config) as graph:
        result = graph.execute(
            "MATCH (m:Message) RETURN m.id, m.embedding_model ORDER BY m.id", {}
        )
    return {row[0]: row[1] for row in result.rows or []}


class TestWhatIsPending:
    def test_everything_the_import_wrote_needs_a_vector(
        self, migrated: GraphConfig
    ) -> None:
        plant(migrated, MESSAGES)

        with client.session(migrated) as graph:
            assert count_pending(graph, MODEL) == 3

    def test_a_message_with_no_body_is_not_pending(self, migrated: GraphConfig) -> None:
        """It is unembeddable, not pending: there is no text. Counting it would
        make the job end reporting a failure nothing can fix."""
        plant(migrated, [*MESSAGES, {"id": "m-4", "subject": "leer", "body": ""}])

        with client.session(migrated) as graph:
            assert count_pending(graph, MODEL) == 3

    def test_a_message_with_no_canonical_id_is_not_pending(
        self, migrated: GraphConfig
    ) -> None:
        plant(migrated, [*MESSAGES, {"id": "", "subject": "kein id", "body": "text"}])

        with client.session(migrated) as graph:
            assert count_pending(graph, MODEL) == 3

    def test_the_body_arrives_already_cut(self, migrated: GraphConfig) -> None:
        """``left()`` truncates in the store: a page is five hundred bodies and
        ``body_clean`` is uncapped, so sending them whole would move tens of
        megabytes to embed the first two thousand characters of each."""
        plant(migrated, [{"id": "m-long", "subject": "s", "body": "x" * 500}])

        with client.session(migrated) as graph:
            [found] = read_pending(graph, model=MODEL, after="", limit=10, max_chars=20)

        assert found.body == "x" * 20


class TestARun:
    async def test_it_writes_a_vector_and_the_model_that_made_it(
        self, migrated: GraphConfig
    ) -> None:
        """Written together and per batch, so a run stopped halfway leaves
        messages that are either fully embedded under a known model or
        untouched — there is no third state for the next run to puzzle over."""
        plant(migrated, MESSAGES)

        run = await embed_pending(sessions(migrated), StubEmbedder(), settings())

        assert (run.total, run.done, run.failed) == (3, 3, 0)
        assert stored_models(migrated) == {
            "m-1": MODEL,
            "m-2": MODEL,
            "m-3": MODEL,
        }

    async def test_a_second_run_over_an_unchanged_archive_does_nothing(
        self, migrated: GraphConfig
    ) -> None:
        """What makes the job safe to re-run after a crash, and what stops a
        scheduled run from re-embedding a hundred thousand messages nightly."""
        plant(migrated, MESSAGES)
        await embed_pending(sessions(migrated), StubEmbedder(), settings())

        embedder = StubEmbedder()
        again = await embed_pending(sessions(migrated), embedder, settings())

        assert (again.total, again.done) == (0, 0)
        assert embedder.texts == ["ping"]

    async def test_changing_the_model_makes_every_message_pending_again(
        self, migrated: GraphConfig
    ) -> None:
        """The reason ``embedding_model`` is on the node. A changed embedder
        does not fail loudly — the vectors it wrote sit in a different space
        and the index cannot tell — so the change has to be *detectable*, and
        this is the query that detects it."""
        plant(migrated, MESSAGES)
        await embed_pending(sessions(migrated), StubEmbedder(), settings())

        run = await embed_pending(
            sessions(migrated), StubEmbedder(model=OTHER_MODEL), settings()
        )

        assert (run.total, run.done) == (3, 3)
        assert set(stored_models(migrated).values()) == {OTHER_MODEL}

    async def test_it_pages_through_a_shrinking_set(
        self, migrated: GraphConfig
    ) -> None:
        """Every page written stops matching the selection, so the set shrinks
        behind the cursor as it walks. With an offset that would skip rows;
        with a cursor it cannot."""
        plant(migrated, MESSAGES)
        embedder = StubEmbedder()

        run = await embed_pending(
            sessions(migrated), embedder, settings(page_size=1, batch_size=1)
        )

        assert run.done == 3
        assert len(embedder.calls) == 4  # the probe, then one per message

    async def test_progress_is_reported_per_batch_and_never_goes_backwards(
        self, migrated: GraphConfig
    ) -> None:
        plant(migrated, MESSAGES)
        seen: list[int] = []

        async def watch(run: EmbedRun) -> None:
            seen.append(run.done)

        await embed_pending(
            sessions(migrated),
            StubEmbedder(),
            settings(page_size=1, batch_size=1),
            on_progress=watch,
        )

        assert seen == sorted(seen)
        assert seen[0] == 0
        assert seen[-1] == 3

    async def test_a_cancelled_run_leaves_what_it_wrote(
        self, migrated: GraphConfig
    ) -> None:
        """Cancellation is not a rollback: the vectors already written are
        real, carry their model, and the next run picks up the rest."""
        plant(migrated, MESSAGES)

        async def cancelled() -> bool:
            return True

        run = await embed_pending(
            sessions(migrated),
            StubEmbedder(),
            settings(page_size=1, batch_size=1),
            cancelled=cancelled,
        )

        assert run.cancelled
        assert run.done == 1
        with client.session(migrated) as graph:
            assert count_pending(graph, MODEL) == 2


class TestTheVectorsAreReallyIndexed:
    async def test_an_embedded_archive_can_be_searched(
        self, migrated: GraphConfig
    ) -> None:
        """The end-to-end claim, and the only one that would catch a vector
        that was written but never indexed: the search has to find it."""
        plant(migrated, MESSAGES)
        embedder = StubEmbedder()
        await embed_pending(sessions(migrated), embedder, settings())

        search = SemanticSearch(sessions(migrated), settings(), embedder)
        result = await search.semantic(
            SearchRequest(
                text="Rechnung Q3\n\nDie Rechnung liegt bei.",
                kind=SearchKind.SEMANTIC,
                limit=1,
            )
        )

        assert [one.message_id for one in result.hits] == ["m-1"]
        assert result.coverage is not None
        assert result.coverage.complete
        assert result.notice == ""

    async def test_coverage_agrees_with_what_the_run_reported(
        self, migrated: GraphConfig
    ) -> None:
        plant(migrated, [*MESSAGES, {"id": "m-4", "subject": "leer", "body": ""}])

        run = await embed_pending(sessions(migrated), StubEmbedder(), settings())

        with client.session(migrated) as graph:
            found = coverage(graph, MODEL)
        assert run.done == found.embedded == 3
        assert found.total == 4  # the bodiless one is in the archive, not in the index


class TestTheRealArchive:
    async def test_the_planted_corpus_is_pending_and_then_embedded(
        self, migrated_archive: GraphConfig
    ) -> None:
        """Against messages written by the real archiver rather than by a
        test: whatever the writer puts in ``body_clean`` is what the selection
        has to work with, and this is the only test that checks the two agree.
        """
        with client.session(migrated_archive) as graph:
            before = count_pending(graph, MODEL)

        run = await embed_pending(
            sessions(migrated_archive), StubEmbedder(), settings()
        )

        with client.session(migrated_archive) as graph:
            after = count_pending(graph, MODEL)
            written = rows_of(graph, catalog.VECTOR_COVERAGE, {"model": MODEL})

        assert before > 0
        assert run.done == before
        assert after == 0
        assert written[0]["embedded"] == run.done


class TestRebuildingTheIndexAtANewLength:
    """The operation a migration cannot give: resizing the index at run time.

    The length of a vector index is fixed when it is built, and it now follows
    a model a human picks on the settings page. Picking OpenAI after Ollama
    means 1536 against a 768 index — and the failure mode is the silent one:
    FalkorDB stores a wrong-length vector, declines to index it, reports no
    error, and every search then finds nothing.
    """

    def test_it_resizes_the_index(self, migrated: GraphConfig) -> None:
        with client.session(migrated) as graph:
            before = vector_index(graph)
        assert before is not None
        assert before.dimension == DIMENSION

        rebuild_index(sessions(migrated), DIMENSION + 4)

        with client.session(migrated) as graph:
            after = vector_index(graph)
        assert after is not None
        assert after.dimension == DIMENSION + 4

    def test_it_forgets_the_vectors_the_old_index_held(
        self, migrated: GraphConfig
    ) -> None:
        """The step it is tempting to skip, and the reason the re-embed works.

        ``MESSAGES_NEEDING_EMBEDDING`` selects on ``embedding_model <> $model``,
        so a message embedded by the *same* model at the *old* length would be
        passed over by the very job meant to replace it — while the vector it
        kept is the wrong length, stored and never indexed.
        """
        plant(migrated, MESSAGES)
        with client.session(migrated) as graph:
            graph.execute(
                "MATCH (m:Message) SET m.embedding = $v, m.embedding_model = $model",
                {"v": [0.1] * DIMENSION, "model": MODEL},
            )
            assert count_pending(graph, MODEL) == 0, "nothing pending to begin with"

        cleared = rebuild_index(sessions(migrated), DIMENSION + 4)

        assert cleared == len(MESSAGES)
        assert set(stored_models(migrated).values()) == {None}
        with client.session(migrated) as graph:
            assert count_pending(graph, MODEL) == len(MESSAGES), (
                "every message is pending again, which is what makes the "
                "re-embed after a resize actually recompute"
            )

    def test_it_leaves_the_message_itself_alone(self, migrated: GraphConfig) -> None:
        """Only the semantic phase's own two properties are touched."""
        plant(migrated, MESSAGES)

        rebuild_index(sessions(migrated), DIMENSION + 4)

        with client.session(migrated) as graph:
            result = graph.execute(
                "MATCH (m:Message) RETURN count(m), count(m.subject), "
                "count(m.body_clean)",
                {},
            )
        assert result.rows[0] == [len(MESSAGES), len(MESSAGES), len(MESSAGES)]

    def test_a_length_of_zero_is_refused_before_anything_is_dropped(
        self, migrated: GraphConfig
    ) -> None:
        """A bad number must not cost the graph the index it already had."""
        with pytest.raises(MailPermanentError, match="positive length"):
            rebuild_index(sessions(migrated), 0)

        with client.session(migrated) as graph:
            still = vector_index(graph)
        assert still is not None
        assert still.dimension == DIMENSION

    def test_it_builds_one_where_there_was_none(self, migrated: GraphConfig) -> None:
        """A graph whose index was dropped is repaired rather than refused.

        ``DROP_VECTOR_INDEX`` is a function now — runic 0.5 emits vector-index
        DDL through ``IndexOperations`` rather than as Cypher a caller can hold
        — so it is *called* with the session where it used to be executed
        against it. What it does to the graph is unchanged, which is what the
        next two lines still measure.
        """
        with client.session(migrated) as graph:
            catalog.DROP_VECTOR_INDEX(graph)
            assert vector_index(graph) is None

        rebuild_index(sessions(migrated), DIMENSION)

        with client.session(migrated) as graph:
            assert vector_index(graph) is not None

    def test_the_shape_matches_the_migration(self) -> None:
        """Two intentional copies of the same constants, kept in step here.

        The migration is a record of what one revision did and must not change;
        these are what a rebuild now should use. Tying them together would let
        a tuning change rewrite history, so they are separate and asserted
        equal instead.
        """
        revisions = Path("graph_migrations/versions")
        text = next(revisions.glob("*vector_index*.py")).read_text()

        assert f'SIMILARITY = "{INDEX_SIMILARITY}"' in text
        assert f"M = {INDEX_M}" in text
        assert f"EF_CONSTRUCTION = {INDEX_EF_CONSTRUCTION}" in text
        assert f"EF_RUNTIME = {INDEX_EF_RUNTIME}" in text
