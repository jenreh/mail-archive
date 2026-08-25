"""Two ways to find a message, and the rule that neither may answer with silence.

Full text always works: it reads the index the baseline migration created and
needs no embedder, no model and no job. Semantic search needs all three, and
when any of them is missing it **raises**. That is the phase's stated
requirement and the reason it exists is worth keeping in front of whoever edits
this file: an empty list is a valid answer to a search, so a user who gets one
concludes their archive holds nothing on the subject and stops looking. Every
one of the states below is a configuration a person can fix in a minute, and
each error says which.

The two paths are kept apart rather than merged behind one "search". Their
scores run in opposite directions — FalkorDB's KNN returns a cosine *distance*
where lower is better, RediSearch a relevance where higher is better — and
their populations differ: full text sees every message, a KNN sees only the
ones carrying a vector under the current model. A single ranked list over both
would be a number nobody computed and a coverage claim nobody checked.

**The caller's words never reach a query parser unescaped.** Cypher is safe
here by construction — the catalogue binds ``$text`` as a parameter — but
RediSearch is a *second* query language behind that parameter, with ``|`` for
OR, a leading ``-`` for negation, ``@field:`` selectors, ``*`` truncation, and
a syntax error on a lone ``(``. The caller may be a model reading through MCP.
So the text is reduced to its words in :func:`searchable_terms` before it goes
anywhere, and what survives is a phrase that can only ever *find* things.

Synchronous except where it cannot be: every runic driver blocks, so the graph
half is ordinary blocking code and an async caller wraps it in
``asyncio.to_thread``. :meth:`SemanticSearch.semantic` is the exception, and
only because embedding the query is an HTTP call — it awaits that, then hands
the blocking half to a thread itself.
"""

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.rows import (
    as_datetime,
    as_float,
    as_int,
    as_text,
    rows_of,
)
from mailarc_analytics.semantic.config import SemanticConfig
from mailarc_analytics.semantic.errors import (
    NO_EMBEDDER,
    NO_FULLTEXT_INDEX,
    NO_VECTOR_INDEX,
    SearchQueryError,
    SemanticError,
    SemanticUnavailable,
    dimension_mismatch,
    nothing_embedded,
)
from mailarc_analytics.semantic.model import (
    IndexOptions,
    SearchHit,
    SearchKind,
    SearchRequest,
    SearchResult,
    SimilarPair,
    VectorCoverage,
)
from mailarc_analytics.semantic.ports import EmbedderPort, EmbedPurpose
from mailarc_core.archive.reader import GraphSessionFactory

logger = logging.getLogger(__name__)

MESSAGE_LABEL = "Message"
EMBEDDING_PROPERTY = "embedding"
"""Where the vectors live. Named once, because three statements and the
migration all have to agree on it."""

VECTOR_KIND = "VECTOR"
FULLTEXT_KIND = "FULLTEXT"

MAX_OVER_FETCH = 2_000
"""The widest a KNN may search, whatever the over-fetch factor works out to.

``k`` costs latency roughly linearly — measured at twenty thousand messages,
0.5 ms at k=10 against 4.7 ms at k=500 — and the over-fetch multiplies the
caller's limit. A ceiling here means a caller asking for two hundred hits
cannot turn a search into a scan of the index.
"""

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
"""A run of letters or digits, in any script.

Written as "not a non-word character and not an underscore" so that German
umlauts and accented names survive — ``[a-z0-9]+`` would cut *Rechnung
Müller* into two useless halves. Everything else, and that is exactly the set
of RediSearch's operators, is dropped rather than escaped: escaping preserves
a caller's intent to negate or to select a field, and a model on the far end of
an MCP tool has no business doing either.
"""


def searchable_terms(text: str) -> str:
    """The caller's words, with every query operator taken out.

    Raises :class:`~mailarc_analytics.semantic.errors.SearchQueryError` when
    nothing searchable is left. That is a different answer from "no matches",
    and the caller can act on it — ``"-@subject:*"`` is a query with no words
    in it, and reporting an empty result would tell them their archive is
    empty.

    The words are re-joined with spaces, which RediSearch reads as **AND**.
    That is the useful default for a mail archive: two words are a narrowing,
    and a caller wanting either can ask twice.
    """
    terms = " ".join(_WORD.findall(text))
    if not terms:
        raise SearchQueryError(
            f"the search {text!r} holds no searchable words — query operators "
            "are removed before the archive sees them, so ask with plain words"
        )
    return terms


def index_options(session: Session) -> tuple[IndexOptions, ...]:
    """Every index the graph really has, as this package needs to read them.

    The store reports one row per label, listing all its indexed properties
    together with a type per property, so a label carrying range, full-text and
    vector indexes at once comes back as a single row. Flattened here to one
    :class:`~mailarc_analytics.semantic.model.IndexOptions` per property and
    kind, because every caller is asking about one property.
    """
    found: list[IndexOptions] = []
    for row in rows_of(session, catalog.VECTOR_INDEX_OPTIONS):
        label = as_text(row.get("label"))
        types = row.get("types")
        options = row.get("options")
        if not isinstance(types, Mapping):
            continue
        for prop, kinds in types.items():
            if VECTOR_KIND not in tuple(kinds or ()):
                found.append(IndexOptions(label=label, prop=str(prop)))
                continue
            settings = _vector_settings(options, str(prop))
            found.append(
                IndexOptions(
                    label=label,
                    prop=str(prop),
                    dimension=as_int(settings.get("dimension")),
                    similarity=as_text(settings.get("similarityFunction")),
                )
            )
    return tuple(found)


def vector_index(session: Session) -> IndexOptions | None:
    """The live vector index on ``Message.embedding``, or ``None``.

    Asked before a search and before an embed job, because the alternative is
    the one failure this area hides: FalkorDB accepts a vector of the wrong
    length, stores it and leaves it out of the index without raising, logging
    or counting an indexing failure. Reading the dimension off the running
    server is the only way to know what the store will actually index.
    """
    for one in index_options(session):
        if one.label == MESSAGE_LABEL and one.prop == EMBEDDING_PROPERTY:
            return one if one.dimension else None
    return None


def has_fulltext_index(session: Session) -> bool:
    """Whether ``Message`` carries the full-text index the baseline creates."""
    for row in rows_of(session, catalog.VECTOR_INDEX_OPTIONS):
        if as_text(row.get("label")) != MESSAGE_LABEL:
            continue
        types = row.get("types")
        if not isinstance(types, Mapping):
            continue
        if any(FULLTEXT_KIND in tuple(kinds or ()) for kinds in types.values()):
            return True
    return False


def coverage(session: Session, model: str) -> VectorCoverage:
    """How many messages carry a vector from *model*, out of how many there are."""
    rows = rows_of(session, catalog.VECTOR_COVERAGE, {"model": model})
    if not rows:
        return VectorCoverage(model=model)
    return VectorCoverage(
        model=model,
        total=as_int(rows[0].get("total")),
        embedded=as_int(rows[0].get("embedded")),
        unembeddable=as_int(rows[0].get("unembeddable")),
    )


def fulltext_hits(session: Session, text: str, *, limit: int) -> tuple[SearchHit, ...]:
    """Messages whose subject or body holds the caller's words, best first.

    The scores come back as RediSearch relevance — unbounded, and meaningless
    across two different queries — so they are scaled against the best hit in
    this result set. That makes the column readable and says what it is: a
    ranking within one answer, not a measurement.

    **An empty answer is checked before it is believed.** Measured on the
    vendored FalkorDB: ``db.idx.fulltext.queryNodes`` against a label with no
    full-text index does not raise — it returns no rows, exactly as a search
    that matched nothing does. So a graph whose migrations were never applied
    would tell every user that their archive holds nothing, forever. The extra
    round trip is paid only when there is nothing to show, which is also the
    only time it could mislead.
    """
    terms = searchable_terms(text)
    rows = _asked(
        session, catalog.FULLTEXT_MESSAGES, {"text": terms, "limit": limit}, terms=terms
    )
    if not rows and not has_fulltext_index(session):
        raise SemanticUnavailable(NO_FULLTEXT_INDEX)
    scores = [as_float(row.get("relevance")) for row in rows]
    best = max(scores, default=0.0)
    return tuple(
        _hit(row, score / best if best > 0 else 0.0)
        for row, score in zip(rows, scores, strict=True)
    )


def knn_hits(
    session: Session, vector: Sequence[float], *, k: int, limit: int, model: str
) -> tuple[SearchHit, ...]:
    """The nearest messages to *vector*, converted to a similarity.

    ``k`` is how wide the index search goes and ``limit`` is what comes back;
    they differ because the procedure cannot be filtered before the fact, so
    every row dropped afterwards has to be over-fetched first.

    *model* is which embedding space the caller is asking in, and it is
    required rather than defaulted because there is no safe default: the index
    holds one vector per message and does not record what produced it, so a
    half-finished re-embed leaves two spaces in one index and an unfiltered KNN
    ranks across both. Passing the model the query vector came from turns an
    embedder change into fewer hits rather than into wrong ones.

    The distance is turned into ``1 - distance`` and clamped. Clamped in both
    directions and not for tidiness: an exact match measured
    ``-1.19e-07`` on a normalised 768-dimensional vector, which renders as
    100.00001 %, and a cosine distance above one — two texts pointing in
    opposite directions — would otherwise come back as a negative similarity.
    """
    rows = rows_of(
        session,
        catalog.SEMANTIC_NEIGHBOURS,
        {"vector": list(vector), "k": k, "limit": limit, "model": model},
    )
    return tuple(_hit(row, _similarity(as_float(row.get("distance")))) for row in rows)


def similar_pairs(
    session: Session, *, model: str, neighbours: int, minimum: float, limit: int
) -> tuple[SimilarPair, ...]:
    """Every pair of messages the index put close together, closest first.

    A2's sixth signal, read in **one** statement rather than one KNN per
    message: :data:`~mailarc_analytics.queries.catalog.SEMANTIC_TOPIC_PAIRS`
    feeds the procedure the vector off each matched node, which was measured to
    work rather than assumed. A hundred thousand round trips is the difference
    between signal 6 being a feature and being a reason not to run the rebuild.

    *neighbours* is how many close messages each message may name. ``k`` asks
    for one more than that, because FalkorDB's KNN returns the query node
    itself first for every message — and the statement then drops it, so the
    caller's number really is neighbours rather than neighbours-minus-one.

    *minimum* is a cosine **similarity** and reaches the store as a distance,
    so no row crosses the wire that the caller would have thrown away. *limit*
    is the ceiling on pairs; the statement orders by distance, so what survives
    a cap is the closest rather than an arbitrary slice.
    """
    rows = rows_of(
        session,
        catalog.SEMANTIC_TOPIC_PAIRS,
        {
            "model": model,
            "k": neighbours + 1,
            "max_distance": 1.0 - minimum,
            "limit": limit,
        },
    )
    return tuple(
        SimilarPair(
            left=as_text(row.get("left")),
            right=as_text(row.get("right")),
            score=_similarity(as_float(row.get("distance"))),
        )
        for row in rows
    )


class SemanticSearch:
    """The archive's two search paths, with one embedder behind one of them.

    Built by the composition root and handed around, the way
    :class:`~mailarc_analytics.queries.reports.AnalyticsReader` is: it holds a
    session factory rather than a session, because a search is complete in
    itself and the page that wants one should not also have to know how a graph
    is opened.

    ``embedder`` is ``None`` whenever
    :attr:`~mailarc_analytics.semantic.config.SemanticConfig.provider` is
    ``none``, which is the default. Every method here works in that state or
    says why it cannot — there is no third option, and in particular no method
    that quietly returns nothing.
    """

    def __init__(
        self,
        graph_session: GraphSessionFactory,
        config: SemanticConfig | None = None,
        embedder: EmbedderPort | None = None,
    ) -> None:
        self._graph_session = graph_session
        self._config = config or SemanticConfig()
        self._embedder = embedder

    @property
    def available(self) -> bool:
        """Whether semantic search can run at all — i.e. is there an embedder.

        A page asks this to decide what to *offer*; it does not replace the
        error, because the index and the vectors can still be missing when the
        answer here is true.
        """
        return self._embedder is not None

    @property
    def model(self) -> str:
        """The embedding model in use, or an empty string when there is none."""
        return self._embedder.model if self._embedder is not None else ""

    def fulltext(self, request: SearchRequest) -> SearchResult:
        """Words, not meanings — the path that needs nothing configured."""
        with self._graph_session() as graph:
            hits = fulltext_hits(graph, request.text, limit=request.limit)
        return SearchResult(kind=SearchKind.FULLTEXT, hits=hits)

    async def semantic(self, request: SearchRequest) -> SearchResult:
        """Messages *about* what was asked for, or a clear reason why not.

        Three things have to be true and each failure names its own remedy:
        an embedder is configured, the vector index exists, and the index holds
        vectors of the dimension this model produces. The third is checked
        against the live index rather than against the configuration, because
        the configuration is exactly what can be wrong — and a mismatch does
        not fail loudly at write time, it silently stops indexing.

        The query is embedded with :attr:`EmbedPurpose.QUERY`, which is not the
        same text a document is embedded with under an instruction-tuned model.
        Everything after that is blocking graph work and goes to a thread.
        """
        embedder = self._require_embedder()
        vector = await self._embed_query(embedder, request.text)
        return await asyncio.to_thread(self._knn, embedder.model, vector, request.limit)

    async def search(self, request: SearchRequest) -> SearchResult:
        """Dispatch on :attr:`SearchRequest.kind` — one entry point for a tool.

        A caller that has a request object and no opinion about paths uses
        this; the two methods above stay public because a page renders the
        two differently and should not have to build a request to say so.
        """
        if request.kind is SearchKind.SEMANTIC:
            return await self.semantic(request)
        return await asyncio.to_thread(self.fulltext, request)

    def coverage(self) -> VectorCoverage:
        """How much of the archive the configured model has embedded.

        Answers for an empty model name when nothing is configured, which
        reads as "nothing is embedded" — true, and the honest thing for a page
        to show beside a disabled search box.
        """
        with self._graph_session() as graph:
            return coverage(graph, self.model)

    def index_dimension(self) -> int:
        """The vector length this graph will actually index, or zero for none.

        Beside :meth:`coverage` and for the same caller: a settings page has to
        be able to say what changing the length would cost, and the only honest
        source for that is the running index. :meth:`_knn` and
        :func:`~mailarc_analytics.semantic.indexing.verify` already refuse
        against this number rather than against
        :attr:`~mailarc_analytics.semantic.config.SemanticConfig.dimension`,
        because the configuration is exactly what can be wrong — a form
        comparing the typed value against the configured one is silent on an
        archive already writing vectors the index drops, and raises a false
        alarm when somebody corrects it.

        Zero rather than ``None`` for "no index at all": both are states a page
        can only warn about, and the two callers that must *refuse* rather than
        warn — the search and the embed job — already have
        :data:`~mailarc_analytics.semantic.errors.NO_VECTOR_INDEX` for it.
        """
        with self._graph_session() as graph:
            index = vector_index(graph)
        return index.dimension if index is not None else 0

    def _require_embedder(self) -> EmbedderPort:
        """The embedder, or the message that says how to get one."""
        if self._embedder is None:
            raise SemanticUnavailable(NO_EMBEDDER)
        return self._embedder

    async def _embed_query(self, embedder: EmbedderPort, text: str) -> Sequence[float]:
        """One vector for the caller's words, checked before it is used.

        A query of the wrong length would be refused by the store with
        ``Vector dimension mismatch`` — a clean error, but one that names
        neither the model nor the setting. Checking here costs nothing and
        produces the message a person can act on.
        """
        vectors = await embedder.embed([text], purpose=EmbedPurpose.QUERY)
        if len(vectors) != 1:
            raise SemanticUnavailable(
                f"{embedder.model} answered {len(vectors)} vectors for one query"
            )
        return vectors[0]

    def _knn(self, model: str, vector: Sequence[float], limit: int) -> SearchResult:
        """The blocking half: check the index, search it, report the coverage.

        Four states are refused here and only the fourth is new. No index, a
        dimension that does not match, and — the one an installation reaches by
        doing everything right except running one job — an index that holds no
        vector from this model at all. The last is checked before the search
        rather than after it because an empty ``hits`` list is a valid answer
        to a search, so answering with one would tell a user their archive
        holds nothing on the subject. An archive with no messages in it is the
        exception and stays an ordinary empty result: nothing is missing, and
        there is no job to recommend.
        """
        with self._graph_session() as graph:
            index = vector_index(graph)
            if index is None:
                raise SemanticUnavailable(NO_VECTOR_INDEX)
            if index.dimension != len(vector):
                raise SemanticUnavailable(
                    dimension_mismatch(
                        index=index.dimension, model=model, produced=len(vector)
                    )
                )
            found = coverage(graph, model)
            if found.total and not found.embedded:
                raise SemanticUnavailable(
                    nothing_embedded(model=model, total=found.total)
                )
            hits = knn_hits(graph, vector, k=self._k(limit), limit=limit, model=model)
        if not found.complete:
            logger.info(
                "Semantic search ran over %d of %d messages: the rest have no "
                "%s embedding yet",
                found.embedded,
                found.total,
                model,
            )
        return SearchResult(kind=SearchKind.SEMANTIC, hits=hits, coverage=found)

    def _k(self, limit: int) -> int:
        """How wide to search the index for *limit* rows worth of answer."""
        return min(max(limit, limit * self._config.knn_over_fetch), MAX_OVER_FETCH)


def _asked(
    session: Session, statement: Statement, params: Mapping[str, Any], *, terms: str
) -> list[dict[str, Any]]:
    """Run a full-text statement, translating what the store refuses.

    The driver's exception for "there is no full-text index on this label" and
    for "that query does not parse" is the same opaque ``ResponseError``, and
    this package must not import ``redis`` to tell them apart — the import
    table has ``mailarc-analytics`` on top of the core and nothing else. So the
    graph is asked which indexes it has, on the error path only, and the answer
    decides which of the two messages the caller gets.
    """
    try:
        return rows_of(session, statement, dict(params))
    except SemanticError:
        raise
    except Exception as error:
        if not has_fulltext_index(session):
            raise SemanticUnavailable(NO_FULLTEXT_INDEX) from error
        raise SearchQueryError(
            f"the archive refused the search {terms!r}: {error}"
        ) from error


def _vector_settings(options: object, prop: str) -> Mapping[str, Any]:
    """The vector options for one property, whichever shape the store used.

    Read defensively on purpose. ``DB.INDEXES`` reports one row per label, and
    a label can carry a range, a full-text and a vector index at once — so the
    options column is either that property's own settings or a mapping keyed by
    property. Guessing wrong would read a dimension of zero and report a
    missing index where there is a working one, so both shapes are accepted and
    anything else is treated as "no settings".
    """
    if not isinstance(options, Mapping):
        return {}
    nested = options.get(prop)
    if isinstance(nested, Mapping):
        return nested
    return options


def _similarity(distance: float) -> float:
    """A cosine distance as a similarity in ``0..1``."""
    return max(0.0, min(1.0, 1.0 - distance))


def _hit(row: Mapping[str, Any], score: float) -> SearchHit:
    """One result row as a value object, with the store's shapes decoded."""
    return SearchHit(
        message_id=as_text(row.get("id")),
        score=score,
        subject=as_text(row.get("subject")),
        sent_at=as_datetime(row.get("sent_at")),
        sender=as_text(row.get("sender")),
    )
