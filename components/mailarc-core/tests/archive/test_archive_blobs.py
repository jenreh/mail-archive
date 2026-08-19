"""The blob store is pure filesystem work, so it is tested against a real one.

No fakes here: the claims worth making — the fan-out, the write-once rule, the
atomic landing — are claims about files, and a fake filesystem would only prove
the fake.
"""

import hashlib
from pathlib import Path

import pytest

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import BlobKind

EML = b"From: a@example.com\r\nSubject: hi\r\n\r\nbody\r\n"
DIGEST = hashlib.sha256(EML).hexdigest()


@pytest.fixture
def store(tmp_path) -> BlobStore:
    return BlobStore(ArchiveConfig(store_dir=tmp_path / "mailstore"))


class TestPaths:
    def test_the_path_fans_out_by_the_first_two_byte_pairs(self, store) -> None:
        """One directory per million messages is a directory nobody can list."""
        path = store.path_for(DIGEST, BlobKind.MESSAGE)

        assert path.parent.name == DIGEST[2:4]
        assert path.parent.parent.name == DIGEST[:2]
        assert path.parent.parent.parent == store.root

    def test_the_file_is_named_by_its_own_digest(self, store) -> None:
        assert store.path_for(DIGEST, BlobKind.MESSAGE).name == f"{DIGEST}.eml"

    def test_a_message_and_an_attachment_are_told_apart_by_the_suffix(
        self, store
    ) -> None:
        assert store.path_for(DIGEST, BlobKind.MESSAGE).suffix == ".eml"
        assert store.path_for(DIGEST, BlobKind.ATTACHMENT).suffix == ".bin"

    def test_a_path_is_named_whether_or_not_anything_is_there(self, store) -> None:
        assert store.exists(DIGEST, BlobKind.MESSAGE) is False
        assert store.path_for(DIGEST, BlobKind.MESSAGE).exists() is False


class TestPut:
    def test_the_bytes_come_back_out_unchanged(self, store) -> None:
        digest = store.put(EML, BlobKind.MESSAGE)

        assert digest == DIGEST
        assert store.read(digest, BlobKind.MESSAGE) == EML
        assert store.exists(digest, BlobKind.MESSAGE) is True

    def test_the_directories_are_created_on_the_way(self, store) -> None:
        assert store.root.exists() is False

        store.put(EML, BlobKind.MESSAGE)

        assert store.path_for(DIGEST, BlobKind.MESSAGE).is_file()

    def test_a_second_put_of_the_same_bytes_does_not_rewrite(self, store) -> None:
        """Write-once is the guarantee, not an optimisation.

        The file is tampered with between the two calls, so a silent rewrite
        would restore the original content and be visible here. Comparing
        timestamps alone would not be: two writes inside one filesystem tick
        can carry the same mtime.
        """
        store.put(EML, BlobKind.MESSAGE)
        path = store.path_for(DIGEST, BlobKind.MESSAGE)
        path.write_bytes(b"tampered")

        digest = store.put(EML, BlobKind.MESSAGE)

        assert digest == DIGEST
        assert path.read_bytes() == b"tampered"

    def test_different_bytes_land_on_different_files(self, store) -> None:
        first = store.put(EML, BlobKind.MESSAGE)
        second = store.put(EML + b"more", BlobKind.MESSAGE)

        assert first != second
        assert store.read(first, BlobKind.MESSAGE) == EML

    def test_the_same_bytes_as_message_and_as_attachment_are_two_files(
        self, store
    ) -> None:
        """The suffix is part of the identity: an .eml is not an attachment."""
        store.put(EML, BlobKind.MESSAGE)

        assert store.exists(DIGEST, BlobKind.ATTACHMENT) is False

        store.put(EML, BlobKind.ATTACHMENT)

        assert store.exists(DIGEST, BlobKind.ATTACHMENT) is True

    def test_empty_bytes_are_storable(self, store) -> None:
        """Zero-length attachments exist in the wild and must not be a crash."""
        digest = store.put(b"", BlobKind.ATTACHMENT)

        assert store.read(digest, BlobKind.ATTACHMENT) == b""

    def test_nothing_partial_is_left_behind(self, store) -> None:
        """The temporary file is an implementation detail, not an artefact."""
        store.put(EML, BlobKind.MESSAGE)

        leftovers = list(store.root.rglob("*.part"))

        assert leftovers == []


class TestAtomicity:
    def test_the_blob_only_appears_once_it_is_complete(
        self, store, monkeypatch
    ) -> None:
        """The bytes go to a temporary file and land with a single rename.

        Observed from inside the rename: by the time the destination gets its
        name, the whole content is already written.
        """
        seen: list[bytes] = []
        real_replace = Path.replace

        def watching_replace(self: Path, target):
            seen.append(self.read_bytes())
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", watching_replace)

        store.put(EML, BlobKind.MESSAGE)

        assert seen == [EML]
        assert store.read(DIGEST, BlobKind.MESSAGE) == EML

    def test_a_failed_landing_leaves_neither_the_blob_nor_a_temporary_file(
        self, store, monkeypatch
    ) -> None:
        """A crash mid-write must leave the whole blob or nothing at all."""

        def exploding_replace(self: Path, target):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "replace", exploding_replace)

        with pytest.raises(OSError, match="disk full"):
            store.put(EML, BlobKind.MESSAGE)

        assert store.exists(DIGEST, BlobKind.MESSAGE) is False
        assert list(store.root.rglob("*.part")) == []


class TestDefaults:
    def test_the_store_falls_back_to_the_configured_default_root(self) -> None:
        assert BlobStore().root == ArchiveConfig().store_dir
