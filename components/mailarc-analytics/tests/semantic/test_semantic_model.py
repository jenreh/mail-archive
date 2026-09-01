"""The value objects, and the two rules that live on them.

Most of this file is ordinary: frozen models, decoded columns, a computed
property. Two things are not, and they are the reason the rules were put on
values rather than in the job that uses them.

:meth:`EmbeddingBatch.assemble` refuses a vector of the wrong length. That
check cannot be delegated to the store, because the store does not perform it:
FalkorDB accepts any length, stores it as a property and leaves it out of the
index without raising or counting a failure. Putting it on a value object is
what makes it testable without a server at all.

:meth:`VectorCoverage.describe` is the sentence every semantic answer carries.
A KNN over a half-embedded archive returns a short, plausible result set that
is indistinguishable from a complete search over a small one, and this is the
only thing that tells them apart.
"""

from datetime import UTC, datetime

import pytest

from mailarc_analytics.semantic.model import (
    DEFAULT_HITS,
    MAX_HITS,
    EmbeddedMessage,
    EmbeddingBatch,
    EmbedRun,
    PendingMessage,
    SearchHit,
    SearchKind,
    SearchRequest,
    SearchResult,
    VectorCoverage,
)

DIMENSION = 4


def pending(*ids: str) -> tuple[PendingMessage, ...]:
    """Messages waiting for a vector, named by id and nothing else."""
    return tuple(PendingMessage(id=one, subject=f"subject {one}") for one in ids)


class TestTheRequest:
    def test_a_limit_of_zero_is_raised_to_one(self) -> None:
        """Clamped rather than refused, because the caller least able to read
        a validation error is the one most likely to send a strange number: a
        model calling through MCP asks for 0 rows or for 10 000, and answering
        "invalid limit" spends a round trip teaching it a bound it could not
        have known."""
        assert SearchRequest(text="invoice", limit=0).limit == 1

    def test_an_enormous_limit_is_cut_to_the_ceiling(self) -> None:
        """The over-fetch multiplies whatever is asked for, so an unbounded
        limit is an index scan wearing a search's clothes."""
        assert SearchRequest(text="invoice", limit=10_000).limit == MAX_HITS

    def test_the_text_is_kept_exactly_as_it_arrived(self) -> None:
        """Turning words into a store query belongs to the module that knows
        which store — putting it here would put a second query language into a
        file that promises no I/O."""
        assert SearchRequest(text="  @subject:(invoice|bill) ").text == (
            "  @subject:(invoice|bill) "
        )

    def test_full_text_is_the_default_path(self) -> None:
        """It is the one that works with nothing configured."""
        request = SearchRequest(text="invoice")

        assert request.kind is SearchKind.FULLTEXT
        assert request.limit == DEFAULT_HITS

    def test_it_is_frozen(self) -> None:
        request = SearchRequest(text="invoice")

        with pytest.raises(ValueError):
            request.text = "something else"  # ty: ignore[invalid-assignment]


class TestAssemblingABatch:
    def test_vectors_are_paired_positionally(self) -> None:
        """By the time a batch gets here, "the n-th vector belongs to the n-th
        message" is already true: Ollama answers in input order and the OpenAI
        adapter has re-sorted by the ``index`` field its API sends."""
        batch = EmbeddingBatch.assemble(
            pending("a", "b"),
            [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
            model="probe",
            dimension=DIMENSION,
        )

        assert [one.id for one in batch.rows] == ["a", "b"]
        assert batch.rows[1].vector == (0.0, 1.0, 0.0, 0.0)
        assert batch.refused == ()

    def test_a_vector_of_the_wrong_length_is_refused_and_named(self) -> None:
        """The failure that hides. FalkorDB would take this vector, store it
        and leave it out of the index — the job would report the message
        embedded and no search would ever return it. Refusing it here is the
        only place it can be caught, and the id is kept so the skip leaves a
        trace rather than a number.
        """
        batch = EmbeddingBatch.assemble(
            pending("good", "short"),
            [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0]],
            model="probe",
            dimension=DIMENSION,
        )

        assert [one.id for one in batch.rows] == ["good"]
        assert batch.refused == ("short",)

    def test_a_short_answer_raises_rather_than_zipping(self) -> None:
        """Zipping would put the wrong vector onto every message after the
        gap. Those writes succeed, the search returns confident nonsense, and
        nothing anywhere records which messages were affected — strictly worse
        than embedding none of them."""
        with pytest.raises(ValueError, match="answered 1 vectors for 2"):
            EmbeddingBatch.assemble(
                pending("a", "b"),
                [[1.0, 0.0, 0.0, 0.0]],
                model="probe",
                dimension=DIMENSION,
            )

    def test_a_row_binds_a_plain_list(self) -> None:
        """A raw statement goes past runic's mapper, so ``vecf32()`` in the
        statement is what turns the list into a vector."""
        row = EmbeddedMessage(id="a", vector=(0.5, 0.25)).as_row()

        assert row == {"id": "a", "vector": [0.5, 0.25]}
        assert isinstance(row["vector"], list)


class TestCoverage:
    def test_a_half_embedded_archive_says_so(self) -> None:
        """The sentence that stops a short result set from reading as "your
        archive holds nothing about this"."""
        found = VectorCoverage(model="nomic-embed-text", total=100, embedded=40)

        assert found.missing == 60
        assert not found.complete
        assert "60 of 100" in found.describe()
        assert "nomic-embed-text" in found.describe()
        assert "embed job" in found.describe()

    def test_a_complete_archive_says_nothing(self) -> None:
        """Empty rather than reassuring, so a caller can append it
        unconditionally — a notice shown every time is a notice nobody reads
        when it matters."""
        assert VectorCoverage(model="m", total=7, embedded=7).describe() == ""

    def test_an_empty_archive_counts_as_complete(self) -> None:
        """There is nothing a search is failing to reach."""
        assert VectorCoverage(model="m").complete

    def test_more_embedded_than_total_never_goes_negative(self) -> None:
        """The two numbers come from one statement and cannot disagree — but a
        negative "missing" would print as a sentence claiming minus three
        messages, and clamping costs nothing."""
        assert VectorCoverage(model="m", total=2, embedded=5).missing == 0


class TestTheResult:
    def test_a_semantic_result_carries_the_notice(self) -> None:
        result = SearchResult(
            kind=SearchKind.SEMANTIC,
            hits=(SearchHit(message_id="a", score=0.9),),
            coverage=VectorCoverage(model="m", total=10, embedded=2),
        )

        assert "8 of 10" in result.notice

    def test_a_full_text_result_has_nothing_to_warn_about(self) -> None:
        """Full text reads the same index for every message, so there is no
        partial state to report."""
        result = SearchResult(kind=SearchKind.FULLTEXT, hits=())

        assert result.coverage is None
        assert result.notice == ""

    def test_a_hit_survives_a_missing_sender_and_date(self) -> None:
        """A report renders a state and never raises: a graph with no
        ``SENT_FROM`` edge for a message is a state."""
        hit = SearchHit(message_id="a")

        assert hit.sender == ""
        assert hit.sent_at is None
        assert hit.subject == ""

    def test_a_hit_keeps_the_moment_it_was_given(self) -> None:
        moment = datetime(2026, 3, 4, 9, 15, tzinfo=UTC)

        assert SearchHit(message_id="a", sent_at=moment).sent_at == moment


class TestTheRun:
    def test_a_fresh_run_counts_nothing_done(self) -> None:
        run = EmbedRun(total=12)

        assert (run.done, run.failed, run.cancelled) == (0, 0, False)

    def test_it_is_frozen_so_progress_is_a_new_value(self) -> None:
        """The job reports progress by making a new run rather than by editing
        one, so a hook that kept a reference cannot see it change underneath."""
        run = EmbedRun(total=12)

        with pytest.raises(ValueError):
            run.done = 5  # ty: ignore[invalid-assignment]
