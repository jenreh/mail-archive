"""Reading the archive back: a listing of messages, and the bytes of one.

The writer's mirror image, and deliberately narrower. It answers three
questions — which messages are here, newest first; which match a search; and
what did this one look like on the wire — and projects all of them onto
values nothing above it has to unpack. A
:class:`~mailarc_core.archive.model.MessageSummary` is a frozen pydantic model,
so a page can hold a list of them after the session that read them is gone.

The graph half is :class:`~mailarc_core.archive.repository.MessageRepository`'s;
this module only opens the session it runs in and turns its rows into
summaries. The disk half is the blob store's.

Synchronous on purpose, like the writer: every runic driver blocks, so an
async caller wraps a call in ``asyncio.to_thread``. Unlike the writer this
owns its session factory rather than taking a session — a read is complete in
itself, and the caller that wants a listing should not also have to know how
a graph is opened.
"""

import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager

from runic.ogm import Session

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.model import (
    Address,
    BlobKind,
    Label,
    Message,
    MessageLabel,
    MessageSummary,
)
from mailarc_core.archive.repository import AddressRepository, MessageRepository
from mailarc_core.archive.search import (
    MessageHit,
    ScoredId,
    SearchFilters,
    SearchPage,
)
from mailarc_core.mail.model import LabelKind

logger = logging.getLogger(__name__)

type GraphSessionFactory = Callable[[], AbstractContextManager[Session]]
"""Opens a runic session. Blocking, so callers reach it from a thread."""

PREVIEW_LENGTH = 160
"""How much body a summary carries — two lines of a list row, not a page."""

_WHITESPACE = re.compile(r"\s+")

_KIND_ORDER = {LabelKind.USER: 0, LabelKind.FOLDER: 1, LabelKind.SYSTEM: 2}
"""A human's labels before the provider's housekeeping."""


class ArchiveReader:
    """The archive as its pages need it: listings, search, bytes, and trust.

    Reads, plus exactly one write — :meth:`trust_remote_content`, the
    annotation a human leaves on an ``Address`` node. It rides here rather
    than in a store of its own because the address *is* what is trusted, and
    the page that asks is the page that already holds this object.
    """

    def __init__(
        self,
        graph_session: GraphSessionFactory,
        blobs: BlobStore,
        preview_length: int = PREVIEW_LENGTH,
    ) -> None:
        self._graph_session = graph_session
        self._blobs = blobs
        self._preview_length = preview_length

    def list_messages(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[MessageSummary]:
        """The newest ``limit`` messages after skipping ``offset``, summarised."""
        with self._graph_session() as graph:
            repository = MessageRepository(graph)
            rows = repository.find_recent(limit=limit, offset=offset)
            return self._summaries(repository, rows)

    def count_messages(self) -> int:
        """How many messages the archive holds."""
        with self._graph_session() as graph:
            return MessageRepository(graph).count()

    def search_messages(
        self, filters: SearchFilters, *, limit: int = 50, offset: int = 0
    ) -> SearchPage:
        """One page of whatever ``filters`` ask for — the search page's one call.

        Three shapes behind one signature, told apart by the filters alone so
        the state above holds no mode flag that could drift from the form:

        * **empty** — the recent listing with its count, exactly what the
          page shows before anyone types;
        * **text** (with or without structured narrowing) — full-text ids
          first, then one hydration pass; ``total`` is ``None`` because
          counting would mean running the procedure again un-paged, and
          ``relevance`` is scaled against the best hit of *this* answer — a
          ranking within one page, not a measurement;
        * **structured only** — the filtered listing plus its count, no
          relevance, because a structural filter matches or does not.

        A ``text`` of operators only — ``"()"`` — raises the sanitizer's
        :class:`ValueError` rather than answering with an empty page that
        would read as an empty archive.
        """
        if filters.empty:
            hits = tuple(
                MessageHit(summary=one)
                for one in self.list_messages(limit=limit, offset=offset)
            )
            return SearchPage(hits=hits, total=self.count_messages())
        with self._graph_session() as graph:
            repository = MessageRepository(graph)
            if filters.text.strip():
                scored = repository.search_fulltext(filters, limit=limit, offset=offset)
                rows = repository.find_by_ids([one.id for one in scored])
                summaries = self._summaries(repository, rows)
                return SearchPage(hits=_ranked(summaries, scored), total=None)
            rows = repository.find_filtered(filters, limit=limit, offset=offset)
            summaries = self._summaries(repository, rows)
            total = repository.count_filtered(filters)
        hits = tuple(MessageHit(summary=one) for one in summaries)
        return SearchPage(hits=hits, total=total)

    def messages_by_ids(self, ids: list[str]) -> list[MessageSummary]:
        """These messages as summaries, in the caller's order.

        The hydration path every *ranked* answer shares: the full-text page
        above and the analytics component's semantic hits both end as a list
        of ids whose order is the ranking. An id the graph no longer holds is
        left out rather than answered with a hole; an empty ask never opens
        a session.
        """
        if not ids:
            return []
        with self._graph_session() as graph:
            repository = MessageRepository(graph)
            return self._summaries(repository, repository.find_by_ids(ids))

    def remote_content_trusted(self, address: str) -> bool:
        """Whether this sender's remote content may load without asking."""
        if not address:
            return False
        with self._graph_session() as graph:
            return AddressRepository(graph).is_remote_trusted(address)

    def trust_remote_content(self, address: str) -> bool:
        """Allow this sender's remote content from now on. ``False`` if the
        archive knows no such address; the session commits on the way out."""
        if not address:
            return False
        with self._graph_session() as graph:
            return AddressRepository(graph).trust_remote(address)

    def raw_message(self, digest: str) -> bytes | None:
        """The original bytes under this digest, or ``None`` if they are gone.

        ``None`` rather than an exception, because a missing blob is a state
        the archive can legitimately be in — the store was moved, or a message
        was archived before the blob store existed — and a viewer answers it
        with a sentence, not a stack trace.
        """
        try:
            return self._blobs.read(digest, BlobKind.MESSAGE)
        except FileNotFoundError:
            logger.warning("No original stored under %s", digest)
            return None

    def _summaries(
        self,
        repository: MessageRepository,
        rows: list[tuple[Message, Address | None]],
    ) -> list[MessageSummary]:
        """A page of rows summarised, labels attached — inside the caller's
        session, one label statement per page, whatever path produced the rows."""
        labels = repository.find_labels([message.id for message, _ in rows])
        return [
            self._summarise(message, sender, labels.get(message.id, []))
            for message, sender in rows
        ]

    def _summarise(
        self, message: Message, sender: Address | None, labels: list[Label]
    ) -> MessageSummary:
        """Inside the session on purpose: a node's fields are read here, once."""
        return MessageSummary(
            id=message.id,
            sender_name=_first(sender.display_names) if sender else "",
            sender_address=sender.id if sender else "",
            subject=message.subject or "",
            preview=preview_of(
                message.body_clean or message.body_text, self._preview_length
            ),
            sent_at=message.sent_at,
            has_attachments=message.has_attachments,
            eml_sha256=message.eml_sha256,
            labels=labels_of(labels),
        )


def _ranked(
    summaries: list[MessageSummary], scored: list[ScoredId]
) -> tuple[MessageHit, ...]:
    """Summaries married to their scores, in score order, scaled to ``0..1``.

    The scaling is the analytics package's rule for the same index: a raw
    RediSearch relevance is unbounded and means nothing across two queries,
    so the best hit of this answer becomes ``1.0`` and the rest follow. All
    zeros stay zero rather than dividing by nothing. The ids drive the loop
    — ``scored`` is the ranking — and a hit whose summary is gone (deleted
    between the two statements) is dropped rather than rendered empty.
    """
    best = max((one.relevance for one in scored), default=0.0)
    named = {summary.id: summary for summary in summaries}
    return tuple(
        MessageHit(
            summary=named[one.id],
            relevance=one.relevance / best if best > 0 else 0.0,
        )
        for one in scored
        if one.id in named
    )


def labels_of(labels: list[Label]) -> tuple[MessageLabel, ...]:
    """Label nodes as a summary carries them: named, and in reading order.

    A node without a name is nothing a chip can print and is left out; a
    node without a kind is taken for a user's, the way the mapping does it.
    """
    named = [
        MessageLabel(name=one.name, kind=one.kind or LabelKind.USER)
        for one in labels
        if one.name
    ]
    return tuple(
        sorted(named, key=lambda one: (_KIND_ORDER[one.kind], one.name.lower()))
    )


def preview_of(body: str | None, length: int = PREVIEW_LENGTH) -> str:
    """The first ``length`` characters of a body, with its whitespace folded.

    Line breaks and indentation are the message's layout, not its content;
    a list row wants one run of words it can clamp.
    """
    if not body:
        return ""
    folded = _WHITESPACE.sub(" ", body).strip()
    if len(folded) <= length:
        return folded
    return folded[:length].rstrip() + "…"


def _first(values: list[str]) -> str:
    return values[0] if values else ""
