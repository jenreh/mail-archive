"""The original bytes, kept beside the graph instead of inside it.

Content-addressed and write-once: the file name *is* the sha256 of what is in
it, so the same message arriving through two accounts, or the same attachment
hanging on twenty mails, is one file. Nothing here ever overwrites — identical
content means an identical name, and different content cannot land on the same
name.

Keeping the whole ``.eml`` matters more than it looks. The graph holds a
capped, parsed rendering of a message; the bytes on disk are the message. A
parser fix can be replayed over the entire archive from here without asking a
provider for anything twice.

Two levels of fan-out by the first two byte pairs, so a million messages spread
over 65 536 leaf directories instead of one unlistable one. Every write goes to
a temporary file in the destination directory and lands with ``os.replace``, so
a crash leaves the whole blob or no blob, never half of one.
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import BlobKind

logger = logging.getLogger(__name__)


class BlobStore:
    """The content-addressed store under ``ArchiveConfig.store_dir``."""

    def __init__(self, config: ArchiveConfig | None = None) -> None:
        self._root = (config or ArchiveConfig()).store_dir

    @property
    def root(self) -> Path:
        """The directory everything is written under."""
        return self._root

    def path_for(self, digest: str, kind: BlobKind) -> Path:
        """Where a blob with this digest lives, whether or not it is there."""
        return self._root / digest[:2] / digest[2:4] / f"{digest}.{kind.value}"

    def exists(self, digest: str, kind: BlobKind) -> bool:
        """Whether these bytes are already stored."""
        return self.path_for(digest, kind).is_file()

    def put(self, data: bytes, kind: BlobKind) -> str:
        """Store bytes and return their sha256; a second call writes nothing.

        Write-once is not an optimisation here, it is the guarantee: an import
        that is retried, or a message that arrives through a second account,
        must not be able to change what is already on disk.
        """
        digest = hashlib.sha256(data).hexdigest()
        path = self.path_for(digest, kind)
        if path.is_file():
            logger.debug("Blob already stored: %s", path.name)
            return digest

        path.parent.mkdir(parents=True, exist_ok=True)
        # The store root holds the original bytes of somebody's actual mail —
        # private data. An explicit chmod rather than a mkdir mode, so a
        # permissive umask cannot leave it listable by other users; with the
        # root closed, the fan-out below it is unreachable however it was made.
        self._root.chmod(0o700)
        _write_atomically(path, data)
        logger.debug("Stored blob %s (%d bytes)", path.name, len(data))
        return digest

    def read(self, digest: str, kind: BlobKind) -> bytes:
        """The stored bytes. Raises ``FileNotFoundError`` if they are not here."""
        return self.path_for(digest, kind).read_bytes()


def _write_atomically(path: Path, data: bytes) -> None:
    """Write to a temporary file in the same directory, then rename it into place.

    Same directory because a rename is only atomic within one filesystem, and
    the system temp directory is routinely a different one. ``fsync`` before
    the rename, so what lands is the whole content and not an empty file the
    page cache had not written yet.
    """
    handle, name = tempfile.mkstemp(dir=path.parent, suffix=".part")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
