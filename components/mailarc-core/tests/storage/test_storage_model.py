"""Tests for the value objects in :mod:`mailarc_core.storage.model`.

No I/O here at all — the numbers are handed in. What is worth pinning down is
the arithmetic a progress bar leans on, and in particular the case the archive
really produces: a directory that does not exist yet, whose filesystem totals
came back as nought. A ratio is the one place that turns into a crash.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mailarc_core.storage import PathUsage, StorageUsage


def measured(
    label: str = "Mailstore",
    used_bytes: int = 250,
    file_count: int = 4,
    total_bytes: int = 1000,
    free_bytes: int = 750,
) -> PathUsage:
    return PathUsage(
        label=label,
        path=Path(".state-agent/mailstore"),
        used_bytes=used_bytes,
        file_count=file_count,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
    )


def edit(usage: PathUsage, field: str, value: int) -> None:
    """Assign through a name the type checker cannot resolve.

    A plain ``usage.used_bytes = 1`` is a static error against a frozen model,
    so the check that pydantic refuses it at *runtime* would never get to run.
    """
    setattr(usage, field, value)


class TestPathUsage:
    def test_the_ratio_is_what_the_path_takes_of_its_filesystem(self) -> None:
        assert measured(used_bytes=250, total_bytes=1000).used_ratio == 0.25

    def test_an_unknown_filesystem_reads_as_nought_not_as_a_crash(self) -> None:
        """The absent-path case: zeros all round, and no ``ZeroDivisionError``."""
        assert measured(used_bytes=0, total_bytes=0).used_ratio == 0.0

    def test_a_measured_path_on_an_unknown_filesystem_is_still_nought(self) -> None:
        assert measured(used_bytes=250, total_bytes=0).used_ratio == 0.0

    def test_the_percentage_is_the_ratio_a_bar_can_be_bound_to(self) -> None:
        assert measured(used_bytes=250, total_bytes=1000).used_percent == 25.0

    def test_it_cannot_be_edited_after_the_fact(self) -> None:
        usage = measured()

        with pytest.raises(ValidationError):
            edit(usage, "used_bytes", 1)


class TestStorageUsage:
    def test_it_keeps_the_paths_in_the_order_it_was_given_them(self) -> None:
        usage = StorageUsage(
            paths=(measured(label="Mailstore"), measured(label="Graph"))
        )

        assert [path.label for path in usage.paths] == ["Mailstore", "Graph"]

    def test_it_totals_what_the_archive_occupies(self) -> None:
        usage = StorageUsage(
            paths=(
                measured(label="Mailstore", used_bytes=250, file_count=4),
                measured(label="Graph", used_bytes=70, file_count=2),
            )
        )

        assert usage.used_bytes == 320
        assert usage.file_count == 6

    def test_an_installation_with_nothing_measured_totals_nought(self) -> None:
        usage = StorageUsage()

        assert usage.used_bytes == 0
        assert usage.file_count == 0
