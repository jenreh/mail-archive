"""Filling in the vectors the import deliberately left empty.

:mod:`mailarc_core.archive.writer` never overwrites an existing ``Message``,
and its docstring says why: the fields the semantic phase fills in are not the
importer's to blank out. This module is the other side of that promise — the
only thing in the project that writes ``Message.embedding`` and
``Message.embedding_model``, and it writes them together, per batch, so that a
run stopped halfway leaves messages that are either fully embedded under a
known model or untouched. There is no third state for the next run to puzzle
over.

**It is the mirror image of** :mod:`mailarc_analytics.derived.rebuild`, and
worth reading with that in mind. A rebuild is blocking work driven from a
thread; embedding is the opposite — the slow part is an awaitable HTTP call and
the graph is the blocking part. So the loop here is plain ``async`` with
``asyncio.to_thread`` around each graph call, and it needs none of the
machinery a reader coming from ``rebuild`` will expect: no sentinel exception,
no bridge back to the loop. Cancellation is an ordinary ``await`` between
pages.

**Between batches, and never inside one.** A batch is one HTTP call and one
write, and the write happens before the check, so a run stopped there has lost
nothing and left nothing half-done. The batch is the unit rather than the page
because a page is *not* seconds on the provider these defaults are tuned for: at
``page_size=500`` and ``batch_size=32`` a page is sixteen embedding calls, and
``request_timeout`` is two minutes because a cold local model really is that
slow — so a check per page could leave somebody who pressed Cancel waiting half
an hour while the job kept writing. Progress moves on the same beat, for the
mirror-image reason: a bar that only advances when a five-hundred-message page
completes shows nothing at all for as long as the page takes.

The transient error is deliberately **not** caught. The job queue already backs
off with jitter and honours ``Retry-After`` (§7.3); a second retry loop
underneath would multiply every wait by a number invisible from the outside —
the argument :class:`~mailarc_google.source.client.GmailClient` makes for not
retrying either.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from itertools import batched

from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import as_int, as_text, rows_of
from mailarc_analytics.semantic.config import SemanticConfig
from mailarc_analytics.semantic.errors import (
    NO_EMBEDDER,
    NO_VECTOR_INDEX,
    SETTINGS_PAGE,
    STORED_WINS,
    SemanticUnavailable,
    dimension_mismatch,
)
from mailarc_analytics.semantic.model import (
    EmbeddingBatch,
    EmbedRun,
    IndexOptions,
    PendingMessage,
)
from mailarc_analytics.semantic.ports import EmbedderPort, EmbedPurpose
from mailarc_analytics.semantic.search import vector_index
from mailarc_core.archive.reader import GraphSessionFactory
from mailarc_core.mail.errors import MailPermanentError

logger = logging.getLogger(__name__)

type EmbedProgress = Callable[[EmbedRun], Awaitable[None]]
"""Told where the run stands after every batch; a job row is the usual reader.

Awaitable for the same reason :data:`CancelCheck` is: the row it moves lives in
SQLite behind an async session, and a synchronous hook would leave the one
caller that matters — the worker — with nothing but a fire-and-forget task per
batch, whose ordering and whose failures are both invisible.
"""

type CancelCheck = Callable[[], Awaitable[bool]]
"""Asked between batches whether to stop. Awaitable, because the answer lives in
SQLite and the caller reaches it the way it reaches everything else."""

PROBE_TEXT = "ping"
"""What :func:`verify` embeds to prove the model is really there.

One short call before a hundred thousand messages. It catches a server that is
not running, a model that was never pulled and a model that quietly produces a
different number of dimensions — three failures that would otherwise be
discovered one batch at a time, or not at all.
"""


def count_pending(session: Session, model: str) -> int:
    """How many messages still need a vector from *model*."""
    rows = rows_of(session, catalog.COUNT_NEEDING_EMBEDDING, {"model": model})
    return as_int(rows[0].get("total")) if rows else 0


def read_pending(
    session: Session, *, model: str, after: str, limit: int, max_chars: int
) -> tuple[PendingMessage, ...]:
    """One page of messages to embed, ordered by id and cut at *max_chars*.

    The cursor is the last id of the previous page, starting at ``""``. Paging
    a set that shrinks as it is walked would be a hazard with an offset and is
    none with a cursor: every write removes rows *behind* the cursor, so no
    page can be skipped and none can repeat.
    """
    rows = rows_of(
        session,
        catalog.MESSAGES_NEEDING_EMBEDDING,
        {"after": after, "model": model, "limit": limit, "max_chars": max_chars},
    )
    return tuple(
        PendingMessage(
            id=as_text(row.get("id")),
            subject=as_text(row.get("subject")),
            body=as_text(row.get("body")),
        )
        for row in rows
    )


def write_batch(session: Session, batch: EmbeddingBatch) -> int:
    """Store one batch's vectors; returns how many nodes were touched.

    The statement's own ``RETURN count(m)`` is the answer rather than the
    driver's write statistics, for the reason
    :func:`~mailarc_analytics.derived.rebuild._drain` gives: those live on a
    private attribute of the raw result and come back as a float.

    An empty batch does not go to the store at all. ``UNWIND []`` is harmless
    but it is still a round trip, and a batch can legitimately be empty after
    every vector in it was refused for its length.
    """
    if not batch.rows:
        return 0
    rows = rows_of(
        session,
        catalog.WRITE_EMBEDDINGS,
        {"rows": [one.as_row() for one in batch.rows], "model": batch.model},
    )
    return as_int(rows[0].get("written")) if rows else 0


def embedding_text(message: PendingMessage, *, max_chars: int) -> str:
    """The text one message is embedded as: its subject, then its clean body.

    The subject is included and comes first because it is the one line a human
    would use to describe the mail, and because a body reduced to a signature
    and a "see attached" would otherwise embed to nothing usable. Cut again
    here although the statement already truncated the body: the subject is
    added afterwards and a long one would push the whole thing over the
    provider's limit.
    """
    joined = f"{message.subject}\n\n{message.body}".strip()
    return joined[:max_chars]


async def verify(
    graph_session: GraphSessionFactory, embedder: EmbedderPort
) -> IndexOptions:
    """Prove a run can succeed before it writes anything. Returns the index.

    Three checks, one graph round trip and one embedding call, and each of them
    stands for a failure that is otherwise silent or expensive:

    1. **Is there a vector index?** Without one the KNN procedure raises an
       opaque driver error at search time — long after the writes.
    2. **Does its dimension match the model's?** This is the one that hides:
       FalkorDB stores a wrong-length vector and declines to index it with no
       error, no log line and no ``indexingFailures`` count, so the job would
       report every message embedded and the search would find none of them.
    3. **Does the model answer at all, with vectors of the length it claims?**
       One short call catches a server that is down, a model that was never
       pulled, and a model whose real dimension differs from the setting.

    The same shape as :meth:`GmailSource.verify` and for the same reason: the
    cheap question is asked once at the start rather than discovered per item.
    """
    index = await asyncio.to_thread(_read_index, graph_session)
    if index is None:
        raise SemanticUnavailable(NO_VECTOR_INDEX)
    if index.dimension != embedder.dimension:
        raise SemanticUnavailable(
            dimension_mismatch(
                index=index.dimension,
                model=embedder.model,
                produced=embedder.dimension,
            )
        )
    probe = await embedder.embed([PROBE_TEXT], purpose=EmbedPurpose.DOCUMENT)
    if len(probe) != 1 or len(probe[0]) != embedder.dimension:
        raise SemanticUnavailable(
            f"{embedder.model} was asked for one {embedder.dimension}-dimensional "
            f"vector and answered {len(probe)} of length "
            f"{len(probe[0]) if probe else 0} — check the model and the length "
            f"on the {SETTINGS_PAGE} page, or app_semantic_model and "
            f"app_semantic_dimension if this archive is configured from a file "
            f"({STORED_WINS})"
        )
    return index


async def embed_pending(
    graph_session: GraphSessionFactory,
    embedder: EmbedderPort | None,
    config: SemanticConfig | None = None,
    *,
    on_progress: EmbedProgress | None = None,
    cancelled: CancelCheck | None = None,
) -> EmbedRun:
    """Embed every message that needs it, a page at a time. Returns what it did.

    Takes ``embedder`` as ``… | None`` on purpose, so that the embedder-off
    case fails **here**, once, with the message that says what to configure —
    rather than in each caller, differently, or not at all. A job that ends
    with "no embedder is configured, set app_semantic_provider" in its error
    column is a job a user can act on.

    Memory is bounded by one page and one batch, not by the archive: five
    hundred truncated bodies plus one batch of vectors is roughly four
    megabytes at the default settings, whether the archive holds a thousand
    messages or a hundred thousand. That is the whole reason the read is paged
    rather than materialised.

    Progress is reported once per batch and never goes backwards: ``total`` is
    counted once before the first page, and ``done`` counts nodes actually
    written, so the number matches what a search can find. A cancel is asked
    for on the same beat, immediately after the batch it could interrupt has
    already been written.
    """
    settings = config or SemanticConfig()
    if embedder is None:
        raise SemanticUnavailable(NO_EMBEDDER)
    await verify(graph_session, embedder)

    total = await asyncio.to_thread(_count, graph_session, embedder.model)
    run = EmbedRun(total=total)
    await _report(on_progress, run)
    after = ""
    while True:
        page = await asyncio.to_thread(
            _page, graph_session, embedder.model, after, settings
        )
        if not page:
            break
        for chunk in batched(page, settings.batch_size, strict=False):
            run = await _embed_chunk(graph_session, embedder, settings, chunk, run)
            await _report(on_progress, run)
            if cancelled is not None and await cancelled():
                logger.info(
                    "Embedding stopped on request after %d of %d messages",
                    run.done,
                    run.total,
                )
                return run.model_copy(update={"cancelled": True})
        after = page[-1].id
    logger.info(
        "Embedded %d messages with %s (%d could not be embedded)",
        run.done,
        embedder.model,
        run.failed,
    )
    return run


async def _embed_chunk(
    graph_session: GraphSessionFactory,
    embedder: EmbedderPort,
    config: SemanticConfig,
    chunk: Sequence[PendingMessage],
    run: EmbedRun,
) -> EmbedRun:
    """One HTTP call and one write; returns the run with the counts moved on.

    Two failures are absorbed here and both leave a trace, because §7.6's rule
    that no skipped message may pass silently applies to a vector as much as to
    an import. A batch the provider refuses permanently — an over-long input, a
    model that does not take the ``dimensions`` argument — costs that batch and
    not the run. An answer that does not line up with the batch that asked for
    it is the same kind of failure one level down, and refusing to guess is the
    point: writing the wrong vector onto a message produces confident nonsense
    that nothing downstream can detect.
    """
    texts = [embedding_text(one, max_chars=config.max_body_chars) for one in chunk]
    try:
        vectors = await embedder.embed(texts, purpose=EmbedPurpose.DOCUMENT)
        batch = EmbeddingBatch.assemble(
            chunk, vectors, model=embedder.model, dimension=embedder.dimension
        )
    except (MailPermanentError, ValueError) as error:
        logger.warning(
            "Skipping %d messages the embedder refused: %s", len(chunk), error
        )
        return run.model_copy(update={"failed": run.failed + len(chunk)})
    if batch.refused:
        logger.warning(
            "Refusing %d vectors of the wrong length from %s: %s",
            len(batch.refused),
            embedder.model,
            ", ".join(batch.refused),
        )
    written = await asyncio.to_thread(_write, graph_session, batch)
    return run.model_copy(
        update={
            "done": run.done + written,
            "failed": run.failed + len(batch.refused) + len(batch.rows) - written,
        }
    )


INDEX_SIMILARITY = "cosine"
INDEX_M = 16
INDEX_EF_CONSTRUCTION = 400
INDEX_EF_RUNTIME = 512
"""The index's shape, matching ``graph_migrations/versions/5f4678dfc5a4``.

Repeated here rather than imported: a migration is a historical record of what
one revision did and must never change once it has been applied anywhere, while
these are what a rebuild *now* should use. Tying them together would mean a
tuning change silently rewriting history. ``tests`` asserts they agree today,
which is the honest way to keep two intentional copies in step.
"""


def rebuild_index(graph_session: GraphSessionFactory, dimension: int) -> int:
    """Rebuild the vector index at ``dimension``, forgetting every vector.

    The operation the settings page needs and a migration cannot give it. The
    length of a vector index is fixed when it is built, and it now follows a
    model a human picks at run time — so changing the embedder has to be able
    to change the index, and a revision with a constant in it cannot express
    "whatever this installation chose".

    The two DDL names are **functions** now rather than statements, which is
    what the call sites below say: runic 0.5 emits vector-index DDL through
    ``IndexOperations`` instead of as Cypher a caller can hold, so
    ``catalog.DROP_VECTOR_INDEX(graph)`` replaces
    ``graph.execute(catalog.DROP_VECTOR_INDEX, {})`` and the five settings that
    used to be ``$parameters`` are keyword arguments a type checker enforces.
    They are the same five values and they still come from the four constants
    above — a call site that forgot one would be a ``TypeError`` here rather
    than an index quietly built with the adapter's own ``efRuntime`` of ten.

    Three steps, in this order and for a reason. The index goes first, because
    FalkorDB will not resize one in place. The vectors go second, and this is
    the step it is tempting to skip: :data:`catalog.MESSAGES_NEEDING_EMBEDDING`
    selects on ``embedding_model <> $model``, so a message embedded by the
    *same* model at the *old* length would be passed over by the very job meant
    to replace it — and the vector it kept is the wrong length, stored and
    never indexed. Clearing is what makes the re-embed actually recompute.
    The new index goes last, so that a failure part-way leaves a graph with no
    index — which every semantic path already refuses loudly — rather than one
    that silently holds vectors of two lengths.

    Ground truth is not touched. ``embedding`` and ``embedding_model`` are the
    semantic phase's own two properties, declared on the node and left empty by
    the import; nothing a message arrived with is read or written here.

    Returns how many messages had a vector taken away, which is what the caller
    tells a human they are about to have to recompute. Blocking, like every
    runic call: an async caller reaches it through ``asyncio.to_thread``.
    """
    if dimension <= 0:
        raise MailPermanentError(
            f"a vector index needs a positive length, not {dimension}"
        )
    with graph_session() as graph:
        existing = vector_index(graph)
        if existing is not None:
            catalog.DROP_VECTOR_INDEX(graph)
        rows = rows_of(graph, catalog.CLEAR_EMBEDDINGS)
        cleared = as_int(rows[0].get("cleared")) if rows else 0
        catalog.CREATE_VECTOR_INDEX(
            graph,
            dimension=dimension,
            similarity=INDEX_SIMILARITY,
            m=INDEX_M,
            ef_construction=INDEX_EF_CONSTRUCTION,
            ef_runtime=INDEX_EF_RUNTIME,
        )
    logger.info(
        "Vector index rebuilt at %d dimensions; %d vectors cleared", dimension, cleared
    )
    return cleared


def _read_index(graph_session: GraphSessionFactory) -> IndexOptions | None:
    """The live vector index, in a session of its own."""
    with graph_session() as graph:
        return vector_index(graph)


def _count(graph_session: GraphSessionFactory, model: str) -> int:
    with graph_session() as graph:
        return count_pending(graph, model)


def _page(
    graph_session: GraphSessionFactory, model: str, after: str, config: SemanticConfig
) -> tuple[PendingMessage, ...]:
    with graph_session() as graph:
        return read_pending(
            graph,
            model=model,
            after=after,
            limit=config.page_size,
            max_chars=config.max_body_chars,
        )


def _write(graph_session: GraphSessionFactory, batch: EmbeddingBatch) -> int:
    with graph_session() as graph:
        return write_batch(graph, batch)


async def _report(hook: EmbedProgress | None, run: EmbedRun) -> None:
    """Tell the caller where the run stands, if anybody asked."""
    if hook is not None:
        await hook(run)
