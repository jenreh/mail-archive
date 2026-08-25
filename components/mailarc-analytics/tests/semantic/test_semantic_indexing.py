"""The embed job without a graph and without a model: the loop and the guards.

Three things are worth testing away from a server, because each of them is
about a decision rather than about a store.

**The start-of-job guard.** Three checks stand between a configuration mistake
and a hundred thousand useless writes, and the second one is the reason this is
not optional: FalkorDB stores a vector of the wrong length and declines to
index it without raising, logging or counting a failure, so a dimension
mismatch does not fail — it disappears.

**What a page costs.** The read is paged and the cursor carries the last id, so
a scripted session that answers a second page and then nothing proves both that
the loop terminates and that it moves.

**What a failure costs.** A batch the provider refuses permanently costs that
batch; a vector of the wrong length costs that message; neither costs the run,
and both leave a count behind rather than silence.
"""

from typing import Any

import pytest
from semantic_stubs import (
    RecordingSession,
    StubEmbedder,
    as_session,
    no_sessions,
    once,
    sessions_from,
    then,
)

from mailarc_analytics.queries import catalog
from mailarc_analytics.semantic.config import SemanticConfig
from mailarc_analytics.semantic.errors import (
    NO_EMBEDDER,
    NO_VECTOR_INDEX,
    SemanticUnavailable,
)
from mailarc_analytics.semantic.indexing import (
    embed_pending,
    embedding_text,
    read_pending,
    verify,
)
from mailarc_analytics.semantic.model import (
    EmbeddedMessage,
    EmbeddingBatch,
    EmbedRun,
    PendingMessage,
)
from mailarc_analytics.semantic.ports import EmbedPurpose
from mailarc_core.mail.errors import MailPermanentError, MailTransientError

DIMENSION = 4
MODEL = "stub-model"


def index_row(dimension: int = DIMENSION) -> dict[str, Any]:
    """One ``DB.INDEXES`` row for a ``Message`` label carrying a vector index.

    The options come back keyed by property, because a label has one index
    structure holding every kind at once — measured against the vendored
    FalkorDB, where ``Message`` carries five range indexes, a full-text index
    and this one together.
    """
    return {
        "label": "Message",
        "properties": ["id", "embedding"],
        "types": {"id": ["RANGE"], "embedding": ["VECTOR"]},
        "options": {
            "id": {},
            "embedding": {"dimension": dimension, "similarityFunction": "cosine"},
        },
    }


def page(*ids: str) -> list[dict[str, Any]]:
    """One page of pending messages."""
    return [
        {"id": one, "subject": f"subject {one}", "body": f"body of {one}"}
        for one in ids
    ]


def graph_answers(
    *,
    dimension: int = DIMENSION,
    total: int = 0,
    pages: list[list[dict[str, Any]]] | None = None,
    written: int | None = None,
) -> dict[str, list[list[dict[str, Any]]]]:
    """A scripted archive: an index, a count, some pages, and a write result."""
    answers: dict[Any, list[list[dict[str, Any]]]] = {
        catalog.VECTOR_INDEX_OPTIONS: once([index_row(dimension)]),
        catalog.COUNT_NEEDING_EMBEDDING: once([{"total": total}]),
    }
    if pages is not None:
        answers[catalog.MESSAGES_NEEDING_EMBEDDING] = then(*pages, [])
    if written is not None:
        answers[catalog.WRITE_EMBEDDINGS] = once([{"written": written}])
    return answers


class TestTheTextAMessageIsEmbeddedAs:
    def test_the_subject_comes_first(self) -> None:
        """It is the line a human would use to describe the mail, and a body
        reduced to "see attached" would otherwise embed to nothing useful."""
        text = embedding_text(
            PendingMessage(id="a", subject="Rechnung Q3", body="Anbei."),
            max_chars=100,
        )

        assert text == "Rechnung Q3\n\nAnbei."

    def test_it_is_cut_again_although_the_statement_already_truncated(self) -> None:
        """The subject is added after the store's ``left()``, so a long one
        would push the pair over the provider's limit."""
        text = embedding_text(
            PendingMessage(id="a", subject="x" * 50, body="y" * 50), max_chars=20
        )

        assert len(text) == 20

    def test_a_message_with_no_subject_is_still_text(self) -> None:
        text = embedding_text(PendingMessage(id="a", body="only a body"), max_chars=100)

        assert text == "only a body"


class TestTheStartOfJobGuard:
    async def test_a_missing_vector_index_stops_the_job(self) -> None:
        """Without one the KNN raises an opaque driver error at search time —
        long after the writes it would have been cheap to refuse."""
        session = RecordingSession({catalog.VECTOR_INDEX_OPTIONS: once([])})

        with pytest.raises(SemanticUnavailable) as caught:
            await verify(sessions_from(session), StubEmbedder())

        assert str(caught.value) == NO_VECTOR_INDEX
        assert "task graph:upgrade" in str(caught.value)

    async def test_a_dimension_mismatch_stops_the_job(self) -> None:
        """The failure that hides. Checked against the *live* index rather
        than against the configuration, because the configuration is exactly
        what can be wrong."""
        session = RecordingSession(
            {catalog.VECTOR_INDEX_OPTIONS: once([index_row(768)])}
        )

        with pytest.raises(SemanticUnavailable, match="768"):
            await verify(sessions_from(session), StubEmbedder(dimension=4))

    async def test_a_model_that_answers_the_wrong_length_stops_the_job(self) -> None:
        """One short call catches a server that is down, a model that was
        never pulled and a model whose real dimension differs from the
        setting — before a hundred thousand messages pay for it."""
        session = RecordingSession(graph_answers())

        with pytest.raises(SemanticUnavailable, match="app_semantic_model"):
            await verify(sessions_from(session), StubEmbedder(truncate=2))

    async def test_a_matching_index_lets_the_job_start(self) -> None:
        session = RecordingSession(graph_answers())

        index = await verify(sessions_from(session), StubEmbedder())

        assert (index.dimension, index.similarity) == (DIMENSION, "cosine")


class TestTheRun:
    async def test_no_embedder_fails_the_job_with_the_readable_message(self) -> None:
        """Better than "no handler registered": the job row then carries a
        sentence naming the setting, which is what a user reads."""
        with pytest.raises(SemanticUnavailable) as caught:
            await embed_pending(no_sessions(), None)

        assert str(caught.value) == NO_EMBEDDER

    async def test_it_walks_pages_until_one_comes_back_empty(self) -> None:
        """Two pages and a stop. The cursor carries the last id forward, so a
        loop that dropped it would ask for the first page forever — which is
        why the script answers a *different* second page and then nothing."""
        session = RecordingSession(
            graph_answers(total=3, pages=[page("a", "b"), page("c")], written=2)
        )
        embedder = StubEmbedder()

        run = await embed_pending(
            sessions_from(session), embedder, SemanticConfig(batch_size=10)
        )

        assert run.total == 3
        assert embedder.texts == [
            "ping",
            "subject a\n\nbody of a",
            "subject b\n\nbody of b",
            "subject c\n\nbody of c",
        ]
        cursors = [
            args["after"]
            for args in session.params_for(catalog.MESSAGES_NEEDING_EMBEDDING)
        ]
        assert cursors == ["", "b", "c"]

    async def test_it_embeds_documents_and_not_queries(self) -> None:
        """The corpus half of the asymmetry an instruction-tuned model is
        trained on. Embedding stored mail as though it were a search would
        quietly cost recall that nothing measures."""
        session = RecordingSession(graph_answers(total=1, pages=[page("a")], written=1))
        embedder = StubEmbedder()

        await embed_pending(sessions_from(session), embedder, SemanticConfig())

        assert [purpose for _, purpose in embedder.calls] == [
            EmbedPurpose.DOCUMENT,
            EmbedPurpose.DOCUMENT,
        ]

    async def test_the_write_binds_the_model_the_vectors_came_from(self) -> None:
        """``embedding_model`` is written together with the vector and never
        apart from it — that pairing is what makes a later model change
        detectable and its recomputation targeted."""
        session = RecordingSession(graph_answers(total=1, pages=[page("a")], written=1))

        await embed_pending(sessions_from(session), StubEmbedder(), SemanticConfig())

        [write] = session.params_for(catalog.WRITE_EMBEDDINGS)
        assert write["model"] == MODEL
        assert [row["id"] for row in write["rows"]] == ["a"]
        assert len(write["rows"][0]["vector"]) == DIMENSION

    async def test_a_batch_is_one_http_call(self) -> None:
        """Five messages at a batch size of two is three calls, not five and
        not one: the size is a setting because a local model on a CPU chokes
        where a paid API does not."""
        session = RecordingSession(
            graph_answers(total=5, pages=[page("a", "b", "c", "d", "e")], written=2)
        )
        embedder = StubEmbedder()

        await embed_pending(
            sessions_from(session), embedder, SemanticConfig(batch_size=2)
        )

        assert [len(texts) for texts, _ in embedder.calls] == [1, 2, 2, 1]

    async def test_progress_counts_what_was_written(self) -> None:
        """Not what was attempted: the number a bar shows has to match what a
        search can actually find."""
        session = RecordingSession(
            graph_answers(total=2, pages=[page("a", "b")], written=2)
        )
        seen: list[tuple[int, int]] = []

        async def watch(one: EmbedRun) -> None:
            seen.append((one.done, one.total))

        run = await embed_pending(
            sessions_from(session),
            StubEmbedder(),
            SemanticConfig(),
            on_progress=watch,
        )

        assert run.done == 2
        assert seen[0] == (0, 2)
        assert seen[-1] == (2, 2)


class TestWhatAFailureCosts:
    async def test_a_permanently_refused_batch_is_counted_and_skipped(self) -> None:
        """One over-long body must not end a run over a hundred thousand
        messages — and §7.6's rule is that the skip leaves a trace, so it is
        counted rather than swallowed."""
        session = RecordingSession(
            graph_answers(total=2, pages=[page("a", "b")], written=0)
        )
        embedder = StubEmbedder(
            error=MailPermanentError("body too long"), misbehave_from=2
        )

        run = await embed_pending(sessions_from(session), embedder, SemanticConfig())

        assert (run.done, run.failed) == (0, 2)

    async def test_a_transient_error_ends_the_run_for_the_queue_to_retry(self) -> None:
        """Deliberately not caught. The job queue already backs off with
        jitter and honours ``Retry-After``; a second retry loop underneath
        would multiply every wait by a number invisible from the outside."""
        session = RecordingSession(graph_answers(total=1, pages=[page("a")], written=0))
        embedder = StubEmbedder(error=MailTransientError("slow down"), misbehave_from=2)

        with pytest.raises(MailTransientError):
            await embed_pending(sessions_from(session), embedder, SemanticConfig())

    async def test_a_vector_of_the_wrong_length_never_reaches_the_store(self) -> None:
        """It would be accepted, stored and left out of the index in silence,
        so the guard has to be on this side of the wire."""
        session = RecordingSession(graph_answers(total=1, pages=[page("a")], written=0))
        embedder = StubEmbedder(truncate=2, misbehave_from=2)

        run = await embed_pending(
            sessions_from(session),
            embedder,
            SemanticConfig(),
        )

        assert (run.done, run.failed) == (0, 1)
        assert session.params_for(catalog.WRITE_EMBEDDINGS) == []


class TestCancellation:
    async def test_it_stops_between_batches_and_says_so(self) -> None:
        """A batch is already durable — it was written before the check — so a
        stop costs nothing there, and the check is between batches rather than
        between pages because a page is not seconds on the provider the
        defaults are tuned for: sixteen calls at a two-minute timeout is half
        an hour of a job that was asked to stop still writing."""
        session = RecordingSession(
            graph_answers(total=3, pages=[page("a"), page("b")], written=1)
        )
        embedder = StubEmbedder()

        async def cancelled() -> bool:
            return True

        run = await embed_pending(
            sessions_from(session),
            embedder,
            SemanticConfig(page_size=1),
            cancelled=cancelled,
        )

        assert run.cancelled
        assert run.done == 1
        assert embedder.texts == ["ping", "subject a\n\nbody of a"]

    async def test_a_cancel_does_not_wait_for_the_rest_of_the_page(self) -> None:
        """The failure the between-pages check hid: one page of four at a
        batch of one meant four embedding calls before anybody asked. At the
        shipped defaults that is sixteen calls of up to two minutes each."""
        session = RecordingSession(
            graph_answers(total=4, pages=[page("a", "b", "c", "d")], written=1)
        )
        embedder = StubEmbedder()

        async def cancelled() -> bool:
            return True

        run = await embed_pending(
            sessions_from(session),
            embedder,
            SemanticConfig(page_size=4, batch_size=1),
            cancelled=cancelled,
        )

        assert run.cancelled
        assert embedder.texts == ["ping", "subject a\n\nbody of a"], (
            "the run kept embedding after it had been asked to stop"
        )

    async def test_progress_moves_once_per_batch_rather_than_once_per_page(
        self,
    ) -> None:
        """A five-hundred-message page that reports only when it finishes
        shows no movement at all for as long as it runs."""
        session = RecordingSession(
            graph_answers(total=3, pages=[page("a", "b", "c")], written=1)
        )
        seen: list[int] = []

        async def watch(run: EmbedRun) -> None:
            seen.append(run.done)

        await embed_pending(
            sessions_from(session),
            StubEmbedder(),
            SemanticConfig(page_size=3, batch_size=1),
            on_progress=watch,
        )

        assert seen == [0, 1, 2, 3]

    async def test_a_run_nobody_cancels_reports_that_too(self) -> None:
        session = RecordingSession(graph_answers(total=1, pages=[page("a")], written=1))

        async def never() -> bool:
            return False

        run = await embed_pending(
            sessions_from(session), StubEmbedder(), SemanticConfig(), cancelled=never
        )

        assert not run.cancelled
        assert run.done == 1


class TestTheReadItself:
    def test_it_binds_the_cursor_the_model_and_the_cut(self) -> None:
        session = RecordingSession(
            {catalog.MESSAGES_NEEDING_EMBEDDING: once(page("a"))}
        )

        found = read_pending(
            as_session(session), model="m", after="zzz", limit=7, max_chars=1_000
        )

        assert found == (PendingMessage(id="a", subject="subject a", body="body of a"),)
        assert session.params_for(catalog.MESSAGES_NEEDING_EMBEDDING) == [
            {"after": "zzz", "model": "m", "limit": 7, "max_chars": 1_000}
        ]

    def test_an_empty_batch_is_never_written(self) -> None:
        """A page whose every vector was refused would otherwise buy a round
        trip to write nothing."""
        from mailarc_analytics.semantic.indexing import write_batch

        session = RecordingSession({})
        empty = EmbeddingBatch(model="m", dimension=DIMENSION)

        assert write_batch(as_session(session), empty) == 0
        assert session.statements() == []

    def test_a_written_batch_reports_what_the_statement_counted(self) -> None:
        """The statement's own ``RETURN count(m)`` and not the driver's write
        statistics: those live on a private attribute and come back as a
        float."""
        from mailarc_analytics.semantic.indexing import write_batch

        session = RecordingSession({catalog.WRITE_EMBEDDINGS: once([{"written": 2}])})
        batch = EmbeddingBatch(
            model="m",
            dimension=DIMENSION,
            rows=(
                EmbeddedMessage(id="a", vector=(1.0, 0.0, 0.0, 0.0)),
                EmbeddedMessage(id="b", vector=(0.0, 1.0, 0.0, 0.0)),
            ),
        )

        assert write_batch(as_session(session), batch) == 2
