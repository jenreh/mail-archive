"""What the semantic layer passes around — values only, no session, no socket.

The same split :mod:`mailarc_analytics.queries.model` makes: a page, an MCP
tool or a job holds these objects long after the graph session that produced
them is closed, and every one of them is frozen so that nothing downstream can
edit a result and hand it on as a finding.

Two of them carry a rule rather than data. :meth:`EmbeddingBatch.assemble` is
where a vector of the wrong length is refused, and it has to be a *value*
operation because that is the only way it can be tested without a server: the
store will not refuse it — FalkorDB accepts any length, stores it and silently
declines to index it. :meth:`VectorCoverage.describe` is the sentence every
semantic answer carries, because a KNN over a half-embedded archive returns a
short, plausible-looking result set that is indistinguishable from "these are
the nearest" when it really means "these are the only ones with vectors".
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator

MAX_HITS = 200
"""The most rows any single search returns.

A ceiling and not a preference. The caller may be a model reading through MCP,
and "give me everything" over a hundred thousand messages is an archive dump
rather than a search — while the over-fetch that makes filtering work
multiplies whatever is asked for by
:attr:`~mailarc_analytics.semantic.config.SemanticConfig.knn_over_fetch`.
"""

DEFAULT_HITS = 20
"""Rows a search returns when the caller does not say. A screenful."""


class SearchKind(StrEnum):
    """Which of the two search paths answered — they are not interchangeable."""

    FULLTEXT = "fulltext"
    """RediSearch over ``subject`` and ``body_text``. Finds the words asked
    for, needs no embedder, and is the only path that always works."""

    SEMANTIC = "semantic"
    """KNN over ``Message.embedding``. Finds mails *about* the thing asked for,
    including ones that never use the word — and needs both an embedder and a
    completed embed job."""


class SearchRequest(BaseModel):
    """One search, as the caller asked for it.

    A value object rather than three parameters because two callers — an MCP
    tool and a page — build the same request from wildly different input, and
    the clamping below has to happen once for both. The text is kept exactly as
    it arrived: turning it into a store query is
    :mod:`~mailarc_analytics.semantic.search`'s business, and doing it here
    would put a second query language into a module that promises no I/O.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    """What the caller typed, unmodified."""

    kind: SearchKind = SearchKind.FULLTEXT
    """Full text unless asked otherwise, because full text always works."""

    limit: int = DEFAULT_HITS
    """Rows wanted, clamped into ``1..MAX_HITS``."""

    @field_validator("limit")
    @classmethod
    def _clamp(cls, value: int) -> int:
        """Bring a limit into range instead of refusing it.

        The caller with the strangest numbers is the one that cannot read a
        validation error: a model calling through MCP will ask for 0 rows or
        for 10 000, and answering "invalid limit" wastes a round trip to teach
        it a bound it could not have known. Clamping answers the question it
        meant to ask.
        """
        return max(1, min(value, MAX_HITS))


class SearchHit(BaseModel):
    """One message a search found, with the fields a result row prints.

    No body. The archive's reader is one call away with the bytes and the
    rendered text, and a hit list that carried bodies would move megabytes to
    show twenty subjects — the KNN over-fetches by an order of magnitude, so
    the cost is paid on rows that are then thrown away.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str
    """The canonical id — what every other read in this project keys on."""

    score: float = 0.0
    """How good the hit is, ``0..1``, higher is better, comparable **only**
    within one :class:`SearchKind`.

    The two paths answer on opposite scales — FalkorDB's KNN returns a cosine
    *distance* where lower is better, RediSearch returns a relevance where
    higher is better — so both are normalised on the way out of
    :mod:`~mailarc_analytics.semantic.search`. Normalised, not merged: a 0.9
    from one path and a 0.9 from the other are not the same claim, and sorting
    a mixed list by this number would invent a ranking neither store made.
    """

    subject: str = ""
    sent_at: datetime | None = None
    sender: str = ""
    """The ``Address`` id the message was sent from, or empty if the graph has
    no ``SENT_FROM`` edge for it."""


class VectorCoverage(BaseModel):
    """How much of the archive the current model has actually embedded.

    Carried by every semantic answer, and the reason is the failure it
    prevents: a message with no vector is not *ranked low* by a KNN, it is
    absent from the index entirely. Ten hits out of an archive where four
    messages in five have no vector look exactly like ten hits out of a fully
    embedded one, and only this number tells them apart.
    """

    model_config = ConfigDict(frozen=True)

    model: str = ""
    """The embedding model the counts are about. A message embedded by another
    model counts as un-embedded here, because that is what it is to a search
    running under this one."""

    total: int = 0
    """Messages with a canonical id — the same population every other count in
    this project uses."""

    embedded: int = 0

    unembeddable: int = 0
    """Messages the embed job will never offer a vector, because there is no
    text to embed.

    An attachment-only mail and a reply that is entirely quoted text both leave
    ``body_clean`` empty, and
    :data:`~mailarc_analytics.queries.catalog.COUNT_NEEDING_EMBEDDING` excludes
    them rather than counting them and failing them. Held separately here so
    that :attr:`complete` means "nothing left that a job could fix": counting
    them as missing left a finished run followed by a "run the embed job"
    notice on every answer forever, which is how a reader learns to ignore the
    notice that matters.
    """

    @property
    def missing(self) -> int:
        """Messages a semantic search cannot reach *and* a job could fix."""
        return max(0, self.total - self.embedded - self.unembeddable)

    @property
    def complete(self) -> bool:
        """Whether running the embed job again would change anything.

        An empty archive is complete: there is nothing a search is failing to
        reach, and reporting a warning for it would train the reader to ignore
        the warning that matters. An archive of messages with no body is
        complete for the same reason — the job has already done everything it
        can, and telling a user to run it again is advice that cannot work.
        """
        return self.missing == 0

    def describe(self) -> str:
        """One sentence for a UI or an MCP result; empty when there is nothing
        to warn about.

        Empty rather than "all messages are embedded", so a caller can append
        it unconditionally and a complete archive says nothing at all.
        """
        if self.complete:
            return ""
        return (
            f"{self.missing} of {self.total} messages have no {self.model} "
            "embedding yet and cannot be found by a semantic search — run the "
            "embed job to include them."
        )


class SimilarPair(BaseModel):
    """Two messages the vector index put close together. A2's sixth signal.

    Deliberately not a
    :class:`~mailarc_analytics.derived.model.SimilarityEdge`, although that is
    what it becomes. This package must stay usable by anything that wants a
    KNN, and the derived layer's vocabulary — ``method``, and what
    ``ABOUT.method`` means — belongs to the analysis rather than to the index.
    The composition root is where the two meet, because it is the one layer
    allowed to name both.

    ``score`` is a similarity in ``0..1``, already clamped, so a caller
    comparing it against a threshold is comparing the number the threshold was
    written for.
    """

    model_config = ConfigDict(frozen=True)

    left: str
    right: str
    score: float


class SearchResult(BaseModel):
    """What a search answers with: the hits, and how much it could see.

    The second half is why this is an object and not a list. A semantic search
    can only find messages that carry a vector, and a message without one is
    absent from the index rather than ranked low — so a result over a
    half-embedded archive is short for a reason the rows themselves cannot
    show. :attr:`notice` is that reason in one sentence, empty when there is
    nothing to say, so a caller can render it unconditionally.
    """

    model_config = ConfigDict(frozen=True)

    kind: SearchKind
    """Which path answered. Carried because the scores mean different things
    and a caller merging two results has to know it is doing so."""

    hits: tuple[SearchHit, ...] = ()

    coverage: VectorCoverage | None = None
    """Only ever set for a semantic result — full text reads the same index for
    every message, so there is no partial state to warn about."""

    @property
    def notice(self) -> str:
        """A sentence to show beside the hits, or an empty string."""
        return "" if self.coverage is None else self.coverage.describe()


class PendingMessage(BaseModel):
    """A message the embed job still owes a vector, with the text to embed.

    ``body`` is already truncated to
    :attr:`~mailarc_analytics.semantic.config.SemanticConfig.max_body_chars`
    when it arrives here: the cut happens in the statement's projection so the
    archive's text never crosses the wire in full, which matters at a page of
    five hundred bodies that may be 64 KB each.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    subject: str = ""
    body: str = ""


class EmbeddedMessage(BaseModel):
    """One computed vector, ready to be written onto its node."""

    model_config = ConfigDict(frozen=True)

    id: str
    vector: tuple[float, ...]

    def as_row(self) -> dict[str, object]:
        """The bound-parameter row
        :data:`~mailarc_analytics.queries.catalog.WRITE_EMBEDDINGS` expects.

        A plain ``list`` and not a :class:`~runic.ogm.Vector`. The reason has
        moved and the conclusion has not: values inside an ``UNWIND`` payload
        never pass through runic's mapper, and ``encode_rows`` converts only
        keys that are *declared field names* — ``vector`` is not one, the
        property is ``embedding`` — so this row would be passed through
        untouched even if it were encoded. What turns the list into a vector is
        the ``vecf32()`` the statement's own converter puts in front of it, and
        a ``Vector`` handed to the driver would arrive as the list it already
        is anyway.
        """
        return {"id": self.id, "vector": list(self.vector)}


class EmbeddingBatch(BaseModel):
    """What one embedding call produced, after the lengths were checked.

    The check cannot be left to the store. FalkorDB takes a vector of any
    length, stores it as a property, and leaves it out of the index without
    raising, logging or counting an indexing failure — measured on the vendored
    runtime. A wrong-length vector is therefore not a write that fails, it is a
    message that quietly stops being findable, and the only place to catch it
    is here, before the write.
    """

    model_config = ConfigDict(frozen=True)

    model: str
    dimension: int
    rows: tuple[EmbeddedMessage, ...] = ()
    refused: tuple[str, ...] = ()
    """Ids whose vector had the wrong length. Kept rather than counted so the
    job can log *which* messages went without one — a skipped message leaves a
    trace, the same rule §7.6 makes for a skipped import."""

    @classmethod
    def assemble(
        cls,
        pending: Sequence[PendingMessage],
        vectors: Sequence[Sequence[float]],
        *,
        model: str,
        dimension: int,
    ) -> Self:
        """Pair messages with their vectors, dropping any of the wrong length.

        Association is **positional**, which is a property of the adapters
        rather than of this function: Ollama answers with a bare list in input
        order and OpenAI answers with an ``index`` on every entry, so
        :class:`~mailarc_analytics.semantic.embedder.OpenAIEmbedder` sorts by
        that index before returning. By the time a batch reaches here, "the
        n-th vector belongs to the n-th message" is already true.

        A count mismatch raises rather than truncating. Zipping a short answer
        onto a long batch would write the *wrong* vector onto every message
        after the gap, which is worse than embedding none of them: the writes
        succeed, the search returns confident nonsense, and nothing anywhere
        says which messages were affected.
        """
        if len(pending) != len(vectors):
            raise ValueError(
                f"the embedder answered {len(vectors)} vectors for {len(pending)} texts"
            )
        rows: list[EmbeddedMessage] = []
        refused: list[str] = []
        for message, vector in zip(pending, vectors, strict=True):
            if len(vector) == dimension:
                rows.append(EmbeddedMessage(id=message.id, vector=tuple(vector)))
            else:
                refused.append(message.id)
        return cls(
            model=model,
            dimension=dimension,
            rows=tuple(rows),
            refused=tuple(refused),
        )


class EmbedRun(BaseModel):
    """Where an embed job stands, or what it ended up doing.

    ``total`` is asked for once, before the first page, and never recomputed:
    an archive that is being imported while it is being embedded would
    otherwise make the progress bar go backwards, which reads as a fault.
    """

    model_config = ConfigDict(frozen=True)

    total: int = 0
    """Messages that needed a vector when the job started."""

    done: int = 0
    """Messages that now carry one — counted from what was written, not from
    what was attempted, so the number matches what a search can find."""

    failed: int = 0
    """Messages this run gave up on: a batch the provider refused permanently,
    or a vector of the wrong length. Never silently zero — every increment is
    logged with a reason."""

    cancelled: bool = False
    """Whether the run stopped between pages because it was asked to."""


class IndexOptions(BaseModel):
    """A vector index as the live graph describes it.

    Read before a job writes anything, because this is the one number that
    cannot be fixed afterwards by a re-run: vectors written against the wrong
    dimension are stored, unindexed and indistinguishable from indexed ones.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    prop: str
    dimension: int = 0
    similarity: str = ""
