"""Reading the archive back: a listing of messages, and the bytes of one.

The writer's mirror image, and deliberately narrower. It answers two questions
— which messages are here, newest first, and what did this one look like on
the wire — and projects both onto values nothing above it has to unpack. A
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
    """The archive as the review page needs it: listings, bytes, and trust.

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
            labels = repository.find_labels([message.id for message, _ in rows])
            return [
                self._summarise(message, sender, labels.get(message.id, []))
                for message, sender in rows
            ]

    def count_messages(self) -> int:
        """How many messages the archive holds."""
        with self._graph_session() as graph:
            return MessageRepository(graph).count()

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
