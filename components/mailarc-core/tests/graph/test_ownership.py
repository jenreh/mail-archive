"""Unit tests for the graph server's ownership record.

The record exists so a later start can tell "somebody else runs this server"
from "this is an orphan of a run that is over". Every test here is about that
one distinction, because getting it wrong in either direction is expensive: a
false orphan means signalling a live installation's server, and a false owner
means the leak stays permanent.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mailarc_core.graph import ownership


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "falkordb"
    directory.mkdir()
    return directory


def _write(data_dir: Path, server: int, supervisor: int, port: int) -> None:
    ownership.owner_path(data_dir).write_text(
        f"{server} {supervisor} {port}\n", encoding="utf-8"
    )


class TestOwnerRecord:
    def test_a_record_survives_the_round_trip(self) -> None:
        entry = ownership.OwnerRecord(server_pid=11, supervisor_pid=22, port=6379)

        assert ownership.OwnerRecord.parsed(entry.rendered) == entry

    @pytest.mark.parametrize("text", ["", "1 2", "1 2 3 4", "one two three", "1 2 x\n"])
    def test_anything_else_is_not_one_of_ours(self, text: str) -> None:
        """A half-written or hand-edited file falls back to plain adoption."""
        assert ownership.OwnerRecord.parsed(text) is None


class TestRecording:
    def test_the_supervisor_written_down_is_this_process(self, data_dir: Path) -> None:
        ownership.record(data_dir, server_pid=4242, port=6379)

        entry = ownership.OwnerRecord.parsed(
            ownership.owner_path(data_dir).read_text(encoding="utf-8")
        )
        assert entry is not None
        assert entry.supervisor_pid == os.getpid()
        assert entry.server_pid == 4242
        assert entry.port == 6379

    def test_a_directory_that_cannot_be_written_is_not_fatal(
        self, tmp_path: Path
    ) -> None:
        """The record buys a cleaner recovery; a start without one is a start."""
        ownership.record(tmp_path / "missing", server_pid=1, port=6379)

    def test_forgetting_one_that_was_never_written_is_allowed(
        self, data_dir: Path
    ) -> None:
        ownership.forget(data_dir)

        assert not ownership.owner_path(data_dir).exists()


class TestAlive:
    def test_this_process_is(self) -> None:
        assert ownership.alive(os.getpid()) is True

    def test_a_reaped_child_is_not(self) -> None:
        """The case the record is read in: a pid that named something once."""
        child = subprocess.Popen([sys.executable, "-c", ""])  # noqa: S603
        child.wait()

        assert ownership.alive(child.pid) is False

    @pytest.mark.parametrize("pid", [0, -1])
    def test_a_pid_that_cannot_name_a_process_is_not(self, pid: int) -> None:
        assert ownership.alive(pid) is False


class TestOrphanPid:
    def test_no_record_means_nothing_to_take_over(self, data_dir: Path) -> None:
        assert ownership.orphan_pid(data_dir, 6379) is None

    def test_a_record_for_another_port_is_not_ours(self, data_dir: Path) -> None:
        """Two installations on one machine share neither port nor data dir."""
        _write(data_dir, server=os.getpid(), supervisor=1, port=6380)

        assert ownership.orphan_pid(data_dir, 6379) is None

    def test_a_live_supervisor_keeps_its_server(
        self, data_dir: Path, monkeypatch
    ) -> None:
        """A second application instance must not stop the first one's server."""
        _write(data_dir, server=4242, supervisor=os.getpid(), port=6379)
        monkeypatch.setattr(ownership, "_is_a_server", lambda pid: True)

        assert ownership.orphan_pid(data_dir, 6379) is None

    def test_a_dead_server_leaves_only_a_stale_file(
        self, data_dir: Path, monkeypatch
    ) -> None:
        _write(data_dir, server=4242, supervisor=4243, port=6379)
        monkeypatch.setattr(ownership, "alive", lambda pid: False)

        assert ownership.orphan_pid(data_dir, 6379) is None

    def test_a_recycled_pid_is_refused(self, data_dir: Path) -> None:
        """This process is alive and is not a redis-server; signalling it would
        be the record's worst possible failure."""
        _write(data_dir, server=os.getpid(), supervisor=999_999, port=6379)

        assert ownership.orphan_pid(data_dir, 6379) is None

    def test_a_live_server_with_a_dead_supervisor_is_an_orphan(
        self, data_dir: Path, monkeypatch
    ) -> None:
        _write(data_dir, server=4242, supervisor=4243, port=6379)
        monkeypatch.setattr(ownership, "alive", lambda pid: pid == 4242)
        monkeypatch.setattr(ownership, "_is_a_server", lambda pid: True)

        assert ownership.orphan_pid(data_dir, 6379) == 4242


class TestIsAServer:
    def test_this_interpreter_is_not_one(self) -> None:
        assert ownership._is_a_server(os.getpid()) is False

    def test_a_pid_that_no_longer_exists_is_not_one(self) -> None:
        assert ownership._is_a_server(999_999) is False

    @pytest.mark.parametrize(
        "reported",
        [
            # macOS: the full path plus the title FalkorDB sets for itself.
            "/opt/mail-archive/falkordb/redis-server 127.0.0.1:6379\n",
            # Linux: the bare command name.
            "redis-server\n",
        ],
    )
    def test_both_platforms_report_a_server(self, monkeypatch, reported: str) -> None:
        """Comparing the whole line cost a take-over the first time this ran:
        the orphan was found, called no server, and adopted un-owned again."""
        monkeypatch.setattr(
            ownership.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=[], returncode=0, stdout=reported, stderr=""
            ),
        )

        assert ownership._is_a_server(4242) is True

    def test_anything_else_is_not_one(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ownership.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="/usr/bin/python3\n", stderr=""
            ),
        )

        assert ownership._is_a_server(4242) is False
