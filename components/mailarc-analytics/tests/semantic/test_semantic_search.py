"""The half of search that needs no server: what it refuses, and what it sends.

The load-bearing test in this file is the first one. With no embedder
configured a semantic search must raise a message that names the setting to
change, and it must raise **before** it opens the graph — which is why the
session factory here fails the test if anything opens one. An empty list would
be the easy implementation and the specific failure the phase's definition of
done forbids: a search that answers "nothing" is a search a user believes.

The second is the tokeniser. Cypher is safe by construction here — the
catalogue binds ``$text`` as a parameter — but behind that parameter sits
RediSearch, a *second* query language with ``|`` for OR, ``-`` for negation,
``@field:`` selectors and a syntax error on a lone ``(``. The caller may be a
model reading through MCP, so the words are reduced to words before they reach
it.
"""

from collections.abc import Sequence
from typing import Any

import pytest
from runic.ogm import Vector
from semantic_stubs import (
    RecordingSession,
    StubEmbedder,
    as_session,
    no_sessions,
    once,
)

from mailarc_analytics.queries import catalog
from mailarc_analytics.semantic.config import SemanticConfig
from mailarc_analytics.semantic.errors import (
    NO_EMBEDDER,
    NO_FULLTEXT_INDEX,
    SearchQueryError,
    SemanticError,
    SemanticUnavailable,
    dimension_mismatch,
)
from mailarc_analytics.semantic.model import SearchKind, SearchRequest
from mailarc_analytics.semantic.ports import EmbedPurpose
from mailarc_analytics.semantic.search import (
    MAX_OVER_FETCH,
    SemanticSearch,
    _similarity,
    fulltext_hits,
    has_fulltext_index,
    index_options,
    searchable_terms,
    vector_index,
)
from mailarc_core.mail.errors import MailError

FULLTEXT_ROW: dict[str, Any] = {
    "label": "Message",
    "properties": ["subject", "body_text"],
    "types": {"subject": ["FULLTEXT"], "body_text": ["FULLTEXT"]},
    "options": {"subject": {}, "body_text": {}},
}
"""One ``DB.INDEXES`` row for a label carrying only the baseline's full-text
index — what an archive that has never been embedded looks like."""

VECTOR_ROW: dict[str, Any] = {
    "label": "Message",
    "properties": ["embedding"],
    "types": {"embedding": ["VECTOR"]},
    "options": {
        "embedding": {"dimension": 768, "similarityFunction": "cosine", "M": 16}
    },
}
"""The same for a vector index, in the shape the vendored FalkorDB answers."""


class TwoVectorEmbedder:
    """An embedder that answers two vectors for one text.

    Not something either shipped adapter can do — both count their answers —
    but the search must not assume that of an implementation it was handed.
    """

    model = "chatty"
    dimension = 4

    async def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbedPurpose = EmbedPurpose.DOCUMENT,
    ) -> Sequence[Vector]:
        return [Vector([1.0, 0.0, 0.0, 0.0]), Vector([0.0, 1.0, 0.0, 0.0])]

    async def aclose(self) -> None:
        return None


def searcher(embedder: StubEmbedder | None = None) -> SemanticSearch:
    """A search whose graph must never be opened."""
    return SemanticSearch(no_sessions(), SemanticConfig(), embedder)


class TestWithNoEmbedder:
    async def test_a_semantic_search_raises_rather_than_answering_nothing(self) -> None:
        """The requirement in one test. An empty result is a valid answer to a
        search, so a user who gets one stops looking — and everything they
        then fail to find was bought by saving an exception."""
        with pytest.raises(SemanticUnavailable) as caught:
            await searcher().semantic(SearchRequest(text="invoice"))

        assert str(caught.value) == NO_EMBEDDER

    async def test_it_raises_before_the_graph_is_ever_opened(self) -> None:
        """``no_sessions`` fails the test if a session is opened, so this
        asserts the order and not merely the outcome: asking the archive first
        and refusing afterwards would be a round trip spent to say no."""
        with pytest.raises(SemanticUnavailable):
            await searcher().search(
                SearchRequest(text="invoice", kind=SearchKind.SEMANTIC)
            )

    def test_the_message_names_the_capability_and_the_cause(self) -> None:
        """Short by choice: which search is off, and what is missing.

        It used to carry the fix as well — the settings page, the two
        providers and their trade-off, the configuration keys. That sentence
        was written for a reader met by a wall of text at the one moment they
        wanted an answer, and the remedy now lives where it can be acted on
        rather than in every surface that reports the state. What a message
        still owes its reader is which capability stopped and why, and that is
        what this pins.
        """
        assert "Semantic search" in NO_EMBEDDER
        assert "no embedder" in NO_EMBEDDER

    def test_it_is_not_a_mail_error(self) -> None:
        """The mail taxonomy answers retry / re-consent / skip, and none of
        them fits. A job catching ``MailTransientError`` must not quietly
        retry "the user has not configured an embedder" forever."""
        assert not issubclass(SemanticUnavailable, MailError)
        assert issubclass(SemanticUnavailable, SemanticError)
        assert issubclass(SearchQueryError, SemanticError)

    def test_the_page_can_ask_whether_to_offer_it_at_all(self) -> None:
        """A page uses this to decide what to render; it does not replace the
        error, because the index and the vectors can still be missing when the
        answer is true."""
        assert not searcher().available
        assert searcher(StubEmbedder()).available

    def test_the_model_reads_as_empty_rather_than_as_a_name(self) -> None:
        assert searcher().model == ""
        assert searcher(StubEmbedder(model="mine")).model == "mine"


class TestTheQueryReachesRediSearchAsWords:
    @pytest.mark.parametrize(
        ("typed", "sent"),
        [
            ("invoice", "invoice"),
            ("  invoice   Q3  ", "invoice Q3"),
            ("invoice|ramen", "invoice ramen"),
            ("-invoice", "invoice"),
            ("@subject:invoice", "subject invoice"),
            ("%invoce%", "invoce"),
            ('"Q3 invoice"', "Q3 invoice"),
            ("in*", "in"),
            ("jens@example.com", "jens example com"),
            ("Rechnung Müller", "Rechnung Müller"),
        ],
    )
    def test_every_operator_is_dropped_and_every_word_survives(
        self, typed: str, sent: str
    ) -> None:
        """Dropped rather than escaped. Escaping would preserve a caller's
        intent to negate or to select a field, and a model on the far end of
        an MCP tool has no business doing either — while ``Müller`` has to
        survive, which is why the pattern is "not a non-word" and not
        ``[a-z0-9]``.
        """
        assert searchable_terms(typed) == sent

    @pytest.mark.parametrize("typed", ["", "   ", "((", "-*", "|||", "@:"])
    def test_a_query_with_no_words_left_says_so(self, typed: str) -> None:
        """A different answer from "no matches", and one the caller can act
        on: reporting an empty result for ``((`` would tell them their archive
        is empty."""
        with pytest.raises(SearchQueryError, match="searchable words"):
            searchable_terms(typed)

    def test_the_lone_bracket_never_reaches_the_parser(self) -> None:
        """RediSearch answers ``Syntax error at offset 0`` for a lone ``(``,
        which would surface as a failed search rather than as a bad query."""
        with pytest.raises(SearchQueryError):
            searchable_terms("(")


class TestTheOverFetch:
    def test_a_search_asks_for_more_rows_than_it_returns(self) -> None:
        """FalkorDB's KNN cannot be filtered before the fact: a ``WHERE``
        after the procedure narrows the *k* rows already chosen, so asking for
        ten and dropping three leaves seven."""
        config = SemanticConfig(knn_over_fetch=10)
        search = SemanticSearch(no_sessions(), config, StubEmbedder())

        assert search._k(20) == 200

    def test_it_is_capped_so_a_search_cannot_become_a_scan(self) -> None:
        """``k`` costs latency roughly linearly, and the over-fetch multiplies
        whatever the caller asked for."""
        config = SemanticConfig(knn_over_fetch=1_000)
        search = SemanticSearch(no_sessions(), config, StubEmbedder())

        assert search._k(200) == MAX_OVER_FETCH

    def test_it_never_asks_for_fewer_rows_than_it_returns(self) -> None:
        """A misconfigured factor of zero would otherwise ask the index for
        nothing and answer nothing, with no error anywhere."""
        config = SemanticConfig(knn_over_fetch=0)
        search = SemanticSearch(no_sessions(), config, StubEmbedder())

        assert search._k(20) == 20


class TestTheMismatchMessage:
    def test_it_names_both_numbers_and_the_consequence(self) -> None:
        """This is the failure that hides: the store takes the wrong-length
        vector, stores it and declines to index it without an error, a log
        line or a failure count."""
        message = dimension_mismatch(index=768, model="big-model", produced=1536)

        assert "768" in message
        assert "1536" in message
        assert "big-model" in message
        assert "never indexed" in message
        assert "app_semantic_dimension" in message


class TestWhatTheStoreRefuses:
    """The driver's own errors, which this package may not import to catch.

    A missing index and an unparseable query arrive as the same opaque
    exception, and ``mailarc-analytics`` cannot tell them apart by type — the
    import table puts it on top of the core and nothing else, so ``redis`` is
    not available to it. It asks the graph which indexes exist instead, on the
    error path only.
    """

    def test_a_refusal_with_the_index_present_is_the_callers_query(self) -> None:
        """Then it is *this* query that cannot run, and another one might."""
        session = RecordingSession(
            {catalog.VECTOR_INDEX_OPTIONS: once([FULLTEXT_ROW])},
            {catalog.FULLTEXT_MESSAGES: RuntimeError("Syntax error at offset 3")},
        )

        with pytest.raises(SearchQueryError, match="Syntax error"):
            fulltext_hits(as_session(session), "invoice", limit=5)

    def test_a_refusal_with_no_index_is_the_missing_migration(self) -> None:
        """A different fix, so a different sentence: the query was fine and the
        schema is behind."""
        session = RecordingSession(
            {catalog.VECTOR_INDEX_OPTIONS: once([])},
            {catalog.FULLTEXT_MESSAGES: RuntimeError("undefined attribute")},
        )

        with pytest.raises(SemanticUnavailable) as caught:
            fulltext_hits(as_session(session), "invoice", limit=5)

        assert str(caught.value) == NO_FULLTEXT_INDEX

    def test_a_full_text_index_is_recognised_where_it_exists(self) -> None:
        session = RecordingSession({catalog.VECTOR_INDEX_OPTIONS: once([FULLTEXT_ROW])})

        assert has_fulltext_index(as_session(session))

    def test_an_index_row_of_an_unexpected_shape_is_ignored(self) -> None:
        """A store that reported its indexes differently would otherwise crash
        a search rather than report a missing index — and the reader here is
        about *schema*, which is exactly where a version difference shows up.
        """
        session = RecordingSession(
            {catalog.VECTOR_INDEX_OPTIONS: once([{"label": "Message", "types": None}])}
        )

        assert index_options(as_session(session)) == ()
        assert vector_index(as_session(session)) is None


class TestReadingTheIndexOptions:
    def test_options_keyed_by_property_are_read(self) -> None:
        """The shape the vendored FalkorDB really answers with: one row per
        label, and the options nested under each property."""
        session = RecordingSession({catalog.VECTOR_INDEX_OPTIONS: once([VECTOR_ROW])})

        index = vector_index(as_session(session))

        assert index is not None
        assert (index.dimension, index.similarity) == (768, "cosine")

    def test_flat_options_are_read_too(self) -> None:
        """Accepted rather than guessed at: a label carrying only a vector
        index could report its settings unnested, and reading zero there would
        report a missing index where a working one stands."""
        flat = {
            "label": "Message",
            "types": {"embedding": ["VECTOR"]},
            "options": {"dimension": 768, "similarityFunction": "cosine"},
        }
        session = RecordingSession({catalog.VECTOR_INDEX_OPTIONS: once([flat])})

        index = vector_index(as_session(session))

        assert index is not None
        assert index.dimension == 768


class TestTheQueryVector:
    async def test_an_embedder_answering_twice_is_refused(self) -> None:
        """One query is one vector. Two would mean the adapter batched
        something, and picking the first would be a guess about which."""
        search = SemanticSearch(no_sessions(), SemanticConfig(), TwoVectorEmbedder())

        with pytest.raises(SemanticUnavailable, match="2 vectors for one query"):
            await search.semantic(SearchRequest(text="invoice"))


class TestTheSimilarityClamp:
    """Two one-line assertions the local suite cannot make.

    ``test_an_exact_match_never_scores_above_one`` names this clamp and cannot
    prove it: the fixture index is four-dimensional, where the arithmetic is
    exact, and the measurement the clamp exists for — a distance of ``-1.19e-07``
    on an identical *768*-dimensional vector — cannot happen there. The upper
    bound was never exercised at all. ``SearchHit.score`` carries no ``ge``/``le``
    constraint, so an unclamped value ships straight to the MCP wire and the
    results table as ``100.00001 %`` or as a negative percentage.
    """

    def test_a_distance_slightly_below_zero_is_still_a_perfect_match(self) -> None:
        assert _similarity(-1.19e-07) == 1.0

    def test_a_distance_above_one_is_not_a_negative_similarity(self) -> None:
        """Two texts pointing in opposite directions. Cosine distance runs to
        2.0, and ``1 - 1.5`` renders as ``-50 %``."""
        assert _similarity(1.5) == 0.0

    def test_an_ordinary_distance_passes_through_untouched(self) -> None:
        """The clamp must not be a floor and a ceiling on everything."""
        assert _similarity(0.25) == pytest.approx(0.75)
