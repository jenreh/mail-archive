"""The archive: the one place ground truth is written.

Everything in the graph that came out of a message is written here and nowhere
else. An analysis may read all of it and add nodes of its own next to it, but
it never edits what a provider actually sent — that separation is what makes a
wrong analysis a rerun instead of a restore.

The graph holds a capped, parsed rendering of each message; the original bytes
go to the blob store beside it. Both are content-addressed, so importing the
same mailbox twice writes nothing the first import did not already write.

One module per concern, layered so nothing points back up:

``model``
    The runic OGM nodes and edges, plus the provenance of one write. No I/O.
``config``
    ``ArchiveConfig`` — where the originals go and how much body the graph keeps.
``blobs``
    ``BlobStore`` — content-addressed, write-once files on disk.
``writer``
    ``MessageArchiver`` — the idempotent upsert into the graph.
"""

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import (
    Account,
    Address,
    ArchivedFrom,
    ArchiveResult,
    ArchiveSource,
    Attachment,
    BlobKind,
    HasAttachment,
    Label,
    Message,
    Thread,
    to_signed_64,
    to_unsigned_64,
)
from mailarc_core.archive.writer import MessageArchiver

__all__ = [
    "Account",
    "Address",
    "ArchiveConfig",
    "ArchiveResult",
    "ArchiveSource",
    "ArchivedFrom",
    "Attachment",
    "BlobKind",
    "BlobStore",
    "HasAttachment",
    "Label",
    "Message",
    "MessageArchiver",
    "Thread",
    "to_signed_64",
    "to_unsigned_64",
]
