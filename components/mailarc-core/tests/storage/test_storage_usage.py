"""Tests for :mod:`mailarc_core.storage.usage`.

Every one of them runs against a `tmp_path` tree. **Never** against `.state`:
that directory is somebody's real mail, and the root ``conftest.py`` fails the
run if it so much as changes while the suite is up.

The interesting cases are not the happy one. A storage panel is asked to render
on an installation whose mailstore has not been created yet, on one whose graph
directory the process may not read, and on a tree somebody dropped a symlink
into — and none of the three may take the reader down or hang the walk.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mailarc_core.storage import StorageReader, directory_bytes


def written(path: Path, payload: bytes) -> Path:
    """A file of a known size, with its parents made."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A nested tree of four files worth 10 + 20 + 30 + 40 bytes."""
    root = tmp_path / "mailstore"
    written(root / "a.eml", b"x" * 10)
    written(root / "b.eml", b"x" * 20)
    written(root / "ab" / "c.eml", b"x" * 30)
    written(root / "ab" / "cd" / "d.eml", b"x" * 40)
    return root


@pytest.fixture
def unreadable(tmp_path: Path) -> Iterator[Path]:
    """A directory the process is not allowed to list."""
    if os.geteuid() == 0:
        pytest.skip("root reads everything; this case cannot be staged")
    path = tmp_path / "falkordb"
    written(path / "dump.rdb", b"x" * 99)
    path.chmod(0o000)
    yield path
    # Restored whatever the test did, or `tmp_path` cleanup inherits the problem.
    path.chmod(0o700)


class TestDirectoryBytes:
    def test_it_counts_every_file_in_the_whole_tree(self, tree: Path) -> None:
        assert directory_bytes(tree) == (4, 100)

    def test_an_empty_directory_is_nought_files_and_nought_bytes(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "mailstore"
        empty.mkdir()

        assert directory_bytes(empty) == (0, 0)

    def test_a_path_that_does_not_exist_is_nought_rather_than_a_failure(
        self, tmp_path: Path
    ) -> None:
        """The fresh-installation case: nothing has written a mailstore yet."""
        assert directory_bytes(tmp_path / "not-created-yet") == (0, 0)

    def test_a_plain_file_counts_as_one_file(self, tmp_path: Path) -> None:
        """The SQLite database is a path the reader is handed like any other."""
        database = written(tmp_path / "mail-archive.db", b"x" * 512)

        assert directory_bytes(database) == (1, 512)

    def test_a_directory_it_may_not_read_is_nought_rather_than_a_failure(
        self, unreadable: Path
    ) -> None:
        assert directory_bytes(unreadable) == (0, 0)

    def test_one_unreadable_subdirectory_does_not_lose_the_rest_of_the_tree(
        self, tree: Path
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root reads everything; this case cannot be staged")
        closed = tree / "ab" / "cd"
        closed.chmod(0o000)
        try:
            assert directory_bytes(tree) == (3, 60)
        finally:
            closed.chmod(0o700)

    def test_a_directory_symlink_is_not_descended_into(self, tree: Path) -> None:
        """A loop back onto the tree must terminate, not count twice and not hang."""
        (tree / "loop").symlink_to(tree, target_is_directory=True)

        assert directory_bytes(tree) == (4, 100)

    def test_a_file_symlink_does_not_drag_in_what_it_points_at(
        self, tree: Path, tmp_path: Path
    ) -> None:
        outside = written(tmp_path / "elsewhere" / "big.eml", b"x" * 5000)
        (tree / "link.eml").symlink_to(outside)

        assert directory_bytes(tree) == (4, 100)


class TestStorageReader:
    def test_it_measures_every_path_it_was_given_and_keeps_their_order(
        self, tree: Path, tmp_path: Path
    ) -> None:
        database = written(tmp_path / "mail-archive.db", b"x" * 512)
        reader = StorageReader({"Mailstore": tree, "Datenbank": database})

        usage = reader.usage()

        assert [path.label for path in usage.paths] == ["Mailstore", "Datenbank"]
        assert [path.used_bytes for path in usage.paths] == [100, 512]
        assert [path.file_count for path in usage.paths] == [4, 1]

    def test_it_reports_the_filesystem_the_path_sits_on(self, tree: Path) -> None:
        usage = StorageReader({"Mailstore": tree}).usage()

        measured = usage.paths[0]
        assert measured.total_bytes > 0
        assert measured.free_bytes > 0
        assert 0.0 <= measured.used_ratio <= 1.0

    def test_a_path_that_does_not_exist_comes_back_as_zeros(
        self, tmp_path: Path
    ) -> None:
        absent = tmp_path / "not-created-yet"
        reader = StorageReader({"Graph": absent})

        measured = reader.usage().paths[0]

        assert measured.path == absent
        assert (measured.used_bytes, measured.file_count) == (0, 0)
        assert (measured.total_bytes, measured.free_bytes) == (0, 0)
        assert measured.used_ratio == 0.0

    def test_one_dead_path_does_not_take_the_reader_down(
        self, tree: Path, tmp_path: Path
    ) -> None:
        reader = StorageReader(
            {"Graph": tmp_path / "not-created-yet", "Mailstore": tree}
        )

        usage = reader.usage()

        assert [path.used_bytes for path in usage.paths] == [0, 100]
        assert usage.used_bytes == 100

    def test_a_reader_with_no_paths_measures_nothing(self) -> None:
        assert StorageReader({}).usage().paths == ()


class TestTheMeasurementIsRemembered:
    """One walk serves every reader for a while, and that is not an optimisation.

    ``StorageReader.usage`` is on the ``on_load`` of ``/`` — the one page that
    needs no sign-in — and one call is a recursive walk of the whole mailstore.
    Uncached, every anonymous request costs thousands of ``stat`` calls against
    a real disk, which is a denial of service anybody can spell.
    """

    def test_a_second_read_inside_the_window_does_not_walk_again(
        self, tree: Path
    ) -> None:
        reader = StorageReader({"Mailstore": tree})
        first = reader.usage()

        written(tree / "e.eml", b"x" * 50)

        assert reader.usage() is first

    def test_the_measurement_is_taken_again_once_it_is_stale(self, tree: Path) -> None:
        reader = StorageReader({"Mailstore": tree}, ttl_seconds=0)
        reader.usage()

        written(tree / "e.eml", b"x" * 50)

        assert reader.usage().used_bytes == 150

    def test_a_caller_can_insist_on_a_fresh_walk(self, tree: Path) -> None:
        """The one place that must not read a remembered number is the panel
        somebody opened *because* they just changed what is on disk."""
        reader = StorageReader({"Mailstore": tree})
        reader.usage()

        written(tree / "e.eml", b"x" * 50)

        assert reader.usage(fresh=True).used_bytes == 150

    def test_the_default_window_is_short_enough_to_stay_a_measurement(self) -> None:
        assert 0 < StorageReader({}).ttl_seconds <= 300
