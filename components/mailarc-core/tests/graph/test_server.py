"""Unit tests for `FalkorDBServer` — the parts that need no real server.

The behaviour that only shows up against a live FalkorDB lives in
`test_server_local.py`, which is skipped without the vendored runtime.
"""

import threading
from pathlib import Path

import pytest

from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.server import FalkorDBServer


def _config(mode: GraphServerMode, **overrides) -> GraphConfig:
    defaults = {"mode": mode, "host": "graph.internal", "port": 6380}
    return GraphConfig(**{**defaults, **overrides})


class TestRemoteMode:
    """A remote server has no lifecycle here — somebody else runs it."""

    def test_start_and_stop_do_nothing_and_never_touch_the_runtime(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # Both would explode if remote mode fell through to the local path:
        # there is no runtime in tmp_path, and nothing is listening.
        monkeypatch.setenv("MAIL_ARCHIVE_FALKORDB_DIR", str(tmp_path / "nothing"))
        server = FalkorDBServer(_config(GraphServerMode.REMOTE))

        server.start()
        server.stop()

        assert server.owned is False
        assert server.startup_error is None

    def test_reports_the_configured_endpoint(self) -> None:
        server = FalkorDBServer(_config(GraphServerMode.REMOTE))

        assert server.endpoint == "graph.internal:6380"


class TestLocalMode:
    def test_a_missing_runtime_is_recorded_as_a_startup_error(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The message has to name the fix, not just the symptom."""
        monkeypatch.setenv("MAIL_ARCHIVE_FALKORDB_DIR", str(tmp_path / "nothing"))
        monkeypatch.setattr(
            "mailarc_core.graph.server.is_serving", lambda host, port: False
        )
        server = FalkorDBServer(
            _config(GraphServerMode.LOCAL, host="127.0.0.1", data_dir=tmp_path / "data")
        )

        with pytest.raises(Exception, match="task tauri:vendor"):
            server.start()

        assert server.startup_error is not None
        assert "task tauri:vendor" in server.startup_error

    def test_an_already_running_server_is_adopted_not_replaced(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Two supervisors on one port must not race to bind it."""
        monkeypatch.setattr(
            "mailarc_core.graph.server.is_serving", lambda host, port: True
        )
        monkeypatch.setattr(
            "mailarc_core.graph.server.subprocess.Popen",
            lambda *args, **kwargs: pytest.fail("must not start a second server"),
        )
        server = FalkorDBServer(
            _config(GraphServerMode.LOCAL, host="127.0.0.1", data_dir=tmp_path / "data")
        )

        server.start()

        assert server.owned is False
        assert server.startup_error is None


class TestAsyncLifecycle:
    """`start` and `stop` block; a caller on an event loop must not.

    The same concern `read_status_async` solves for status: the graph package
    knows its own calls block, so it — not its callers — owns the thread hop.
    """

    async def test_start_runs_off_the_event_loop(self, monkeypatch) -> None:
        server = FalkorDBServer(_config(GraphServerMode.REMOTE))
        ran_on: list[int] = []
        monkeypatch.setattr(
            server, "start", lambda: ran_on.append(threading.get_ident())
        )

        await server.start_async()

        assert ran_on == [ran_on[0]]
        assert ran_on[0] != threading.get_ident()

    async def test_stop_runs_off_the_event_loop(self, monkeypatch) -> None:
        server = FalkorDBServer(_config(GraphServerMode.REMOTE))
        ran_on: list[int] = []
        monkeypatch.setattr(
            server, "stop", lambda: ran_on.append(threading.get_ident())
        )

        await server.stop_async()

        assert ran_on == [ran_on[0]]
        assert ran_on[0] != threading.get_ident()

    async def test_a_failed_start_still_raises_across_the_thread(
        self, monkeypatch
    ) -> None:
        """Whether a broken server is fatal is the caller's policy, not ours."""

        def boom() -> None:
            raise RuntimeError("run `task tauri:vendor`")

        server = FalkorDBServer(_config(GraphServerMode.LOCAL))
        monkeypatch.setattr(server, "start", boom)

        with pytest.raises(RuntimeError, match="task tauri:vendor"):
            await server.start_async()
