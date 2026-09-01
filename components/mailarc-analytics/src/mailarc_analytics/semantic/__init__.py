"""Vectors: where they come from, how they get onto the nodes, and who reads them.

The one place in this project where a model touches the archive — and it may
only ever *add a vector*. §3.2 draws the line and this package sits exactly on
it: email already carries an exact graph in its headers, so nothing here
extracts entities, infers relationships or writes a node. ``runic.rag`` is
banned repository-wide and the isolation probe checks it; what is left is two
small HTTP adapters and a KNN.

Five modules, one job each:

``config``
    :class:`~mailarc_analytics.semantic.config.SemanticConfig`, whose
    ``provider`` defaults to ``none``. That default is the feature: without an
    embedder A1-A3 run in full and only semantic search and A2's sixth signal
    are missing, which is what keeps the desktop application free of
    prerequisites. Beside it,
    :class:`~mailarc_analytics.semantic.config.SemanticOverrides` — the five of
    those settings a human may change at runtime, each optional, and the rule
    that an unset one falls through to the file.
``ports``
    :class:`~mailarc_analytics.semantic.ports.EmbedderPort`. The second seam in
    the project that clears the bar of "a port needs a second implementation" —
    both exist on day one, and the choice between them is a decision about
    privacy rather than about a backend.
``embedder``
    The two adapters and :func:`~mailarc_analytics.semantic.embedder.
    build_embedder`, which answers ``None`` when nothing is configured. A null
    object would have been the tidier shape and is exactly wrong here: it would
    make every surface look as though it worked and return nothing.
``search``
    Full text and KNN, kept apart because their scores run in opposite
    directions and they see different populations.
``indexing``
    The embed job's half of the work: page the archive, embed, write the
    vectors back beside the ground truth without touching it.

``model`` and ``errors`` hold the values and the messages the five share.

The rule that binds all of them: **with no embedder configured, nothing here
returns an empty result to mean "not available"**. A search raises
:class:`~mailarc_analytics.semantic.errors.SemanticUnavailable` carrying
:data:`~mailarc_analytics.semantic.errors.NO_EMBEDDER`, which names the setting
to change. An empty list is a valid answer to a search, and a user who reads
one believes their archive holds nothing on the subject.
"""

from mailarc_analytics.semantic.config import (
    SemanticConfig,
    SemanticControl,
    SemanticOverrides,
    SemanticProvider,
)
from mailarc_analytics.semantic.embedder import (
    NATIVE_DIMENSIONS,
    native_dimension,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    OllamaEmbedder,
    OpenAIEmbedder,
    build_embedder,
)
from mailarc_analytics.semantic.errors import (
    NO_EMBEDDER,
    NO_FULLTEXT_INDEX,
    NO_VECTOR_INDEX,
    SETTINGS_PAGE,
    STORED_WINS,
    SearchQueryError,
    SemanticError,
    SemanticUnavailable,
    dimension_mismatch,
)
from mailarc_analytics.semantic.indexing import (
    rebuild_index,
    PROBE_TEXT,
    CancelCheck,
    EmbedProgress,
    count_pending,
    embed_pending,
    embedding_text,
    read_pending,
    verify,
    write_batch,
)
from mailarc_analytics.semantic.model import (
    DEFAULT_HITS,
    MAX_HITS,
    EmbeddedMessage,
    EmbeddingBatch,
    EmbedRun,
    IndexOptions,
    PendingMessage,
    SearchHit,
    SearchKind,
    SearchRequest,
    SearchResult,
    SimilarPair,
    VectorCoverage,
)
from mailarc_analytics.semantic.ports import EmbedderPort, EmbedPurpose
from mailarc_analytics.semantic.search import (
    MAX_OVER_FETCH,
    SemanticSearch,
    coverage,
    fulltext_hits,
    has_fulltext_index,
    index_options,
    knn_hits,
    searchable_terms,
    similar_pairs,
    vector_index,
)

__all__ = [
    "DEFAULT_BASE_URLS",
    "DEFAULT_HITS",
    "DEFAULT_MODELS",
    "MAX_HITS",
    "MAX_OVER_FETCH",
    "NATIVE_DIMENSIONS",
    "NO_EMBEDDER",
    "NO_FULLTEXT_INDEX",
    "NO_VECTOR_INDEX",
    "PROBE_TEXT",
    "SETTINGS_PAGE",
    "STORED_WINS",
    "CancelCheck",
    "EmbedProgress",
    "EmbedPurpose",
    "EmbedRun",
    "EmbeddedMessage",
    "EmbedderPort",
    "EmbeddingBatch",
    "IndexOptions",
    "OllamaEmbedder",
    "OpenAIEmbedder",
    "PendingMessage",
    "SearchHit",
    "SearchKind",
    "SearchQueryError",
    "SearchRequest",
    "SearchResult",
    "SemanticConfig",
    "SemanticControl",
    "SemanticError",
    "SemanticOverrides",
    "SemanticProvider",
    "SemanticSearch",
    "SemanticUnavailable",
    "SimilarPair",
    "VectorCoverage",
    "build_embedder",
    "count_pending",
    "coverage",
    "dimension_mismatch",
    "embed_pending",
    "embedding_text",
    "fulltext_hits",
    "has_fulltext_index",
    "index_options",
    "knn_hits",
    "native_dimension",
    "read_pending",
    "rebuild_index",
    "searchable_terms",
    "similar_pairs",
    "vector_index",
    "verify",
    "write_batch",
]
