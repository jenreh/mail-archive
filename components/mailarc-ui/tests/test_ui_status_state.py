"""The graph status panel: what it reads, what it survives, and when it stops.

Moved here with the state itself. It used to live in ``tests/states/`` because
the state lived in ``app/states/`` — it imported ``app.composition`` directly
for the two things it needed, which is precisely what a component may not do.
:class:`~mailarc_core.graph.health.GraphHealth` is the façade that ended that,
so there is one collaborator to stand in for rather than two module-level
functions to patch.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from mailarc_core import (
    GraphInfo,
    GraphServerMode,
    GraphServerStatus,
    ServerMetrics,
)
from mailarc_core.graph.health import GraphHealth
from mailarc_ui.status.state import GraphStatusState, format_uptime

CHECKED_AT = datetime(2026, 8, 18, 14, 30, 5, tzinfo=UTC)

STATE_MODULE = "mailarc_ui.status.state"


async def _run_poll(state: GraphStatusState) -> None:
    """Invoke the background handler's underlying coroutine.

    Reflex refuses a direct `state.poll()` call on a background handler, so go
    through the EventHandler's wrapped function.
    """
    await GraphStatusState.poll.fn(state)  # ty: ignore[unresolved-attribute]


def _reachable() -> GraphServerStatus:
    return GraphServerStatus(
        mode=GraphServerMode.LOCAL,
        endpoint="127.0.0.1:6379",
        reachable=True,
        checked_at=CHECKED_AT,
        redis_version="8.10.1",
        falkordb_version="4.20.3",
        latency_ms=1.234,
        metrics=ServerMetrics(
            uptime_seconds=3725,
            used_memory_human="1.41M",
            connected_clients=2,
            total_commands_processed=1234567,
        ),
        graphs=(
            GraphInfo(name="mail-archive", node_count=1234, edge_count=99),
            GraphInfo(name="broken", node_count=-1, edge_count=-1),
        ),
    )


def _health(read: Any, startup_error: str | None = None) -> Any:
    """A stand-in for the façade the composition root publishes.

    ``spec=GraphHealth`` rather than a bare mock, so a rename on the real class
    fails these tests instead of leaving them passing against an object the
    application no longer has.
    """
    health = MagicMock(spec=GraphHealth)
    health.status = read
    health.startup_error = Mock(return_value=startup_error)
    return health


@pytest.fixture
def state() -> GraphStatusState:
    """The panel as a page drives it."""
    return GraphStatusState()


def _scripted(state, *results):
    """Feed the poll loop a fixed sequence of readings, then stop it.

    Exceptions in the sequence are raised; once the sequence is exhausted the
    next call clears `polling`, which ends the loop.
    """
    remaining = list(results)
    calls = {"n": 0}

    async def next_result():
        calls["n"] += 1
        if not remaining:
            state.polling = False
            return _reachable()
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return AsyncMock(side_effect=next_result), calls


class _MultiPatch:
    def __init__(self, *patchers):
        self._patchers = patchers

    def __enter__(self):
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *exc_info):
        for patcher in reversed(self._patchers):
            patcher.stop()
        return False


def _patched_loop(read):
    """Patch the collaborator plus the state lock the background task takes."""
    return _MultiPatch(
        patch(f"{STATE_MODULE}.graph_health", return_value=_health(read)),
        patch.object(GraphStatusState, "__aenter__", AsyncMock()),
        patch.object(GraphStatusState, "__aexit__", AsyncMock(return_value=False)),
    )


def _patch_status(status: GraphServerStatus, startup_error: str | None = None):
    return patch(
        f"{STATE_MODULE}.graph_health",
        return_value=_health(AsyncMock(return_value=status), startup_error),
    )


def _patch_status_raising(error: type[Exception]):
    return patch(
        f"{STATE_MODULE}.graph_health",
        return_value=_health(AsyncMock(side_effect=error)),
    )


class TestTheLookup:
    def test_it_says_what_is_missing_rather_than_raising_a_key_error(self) -> None:
        """The same sentence ``analytics_reader`` gives, for the same reason: a
        bare ``KeyError`` names a registry nobody reading a status page has
        heard of."""
        from mailarc_ui.status.state import graph_health

        registry = MagicMock()
        registry.get.side_effect = KeyError("GraphHealth")
        with (
            patch(f"{STATE_MODULE}.service_registry", return_value=registry),
            pytest.raises(RuntimeError, match=r"app\.composition"),
        ):
            graph_health()


class TestRefresh:
    async def test_populates_every_field_from_a_reachable_server(self, state) -> None:
        with _patch_status(_reachable()):
            await state.refresh()

        assert state.checked is True
        assert state.reachable is True
        assert state.mode == "local"
        assert state.endpoint == "127.0.0.1:6379"
        assert state.redis_version == "8.10.1"
        assert state.falkordb_version == "4.20.3"
        assert state.knn_supported is True
        assert state.latency == "1.2 ms"
        # Displayed in the viewer's local zone, not the UTC the domain stores.
        assert state.checked_at == CHECKED_AT.astimezone().strftime("%H:%M:%S")
        assert state.error == ""
        assert state.loading is False

    async def test_projects_metrics_for_display(self, state) -> None:
        with _patch_status(_reachable()):
            await state.refresh()

        assert state.uptime == "1h 2m"
        assert state.used_memory == "1.41M"
        assert state.connected_clients == "2"
        assert state.commands_processed == "1,234,567"

    async def test_projects_the_graph_inventory(self, state) -> None:
        with _patch_status(_reachable()):
            await state.refresh()

        assert [(g.name, g.nodes, g.edges) for g in state.graphs] == [
            ("mail-archive", "1,234", "99"),
            ("broken", "?", "?"),
        ]
        assert state.has_graphs is True

    async def test_an_unreachable_server_surfaces_the_error(self, state) -> None:
        unreachable = GraphServerStatus.unreachable(
            mode=GraphServerMode.REMOTE,
            endpoint="graph.internal:6379",
            checked_at=CHECKED_AT,
            error="ConnectionError: refused",
        )

        with _patch_status(unreachable):
            await state.refresh()

        assert state.reachable is False
        assert state.error == "ConnectionError: refused"
        assert state.knn_supported is False
        assert state.graphs == []
        assert state.loading is False

    async def test_a_startup_failure_is_preferred_over_the_symptom(self, state) -> None:
        """ "Connection refused" is useless next to "run `task tauri:vendor`"."""
        unreachable = GraphServerStatus.unreachable(
            mode=GraphServerMode.LOCAL,
            endpoint="127.0.0.1:6379",
            checked_at=CHECKED_AT,
            error="ConnectionError: refused",
        )

        with _patch_status(
            unreachable,
            startup_error="FalkorDB runtime incomplete — run `task tauri:vendor`",
        ):
            await state.refresh()

        assert "task tauri:vendor" in state.error

    async def test_the_startup_error_is_not_read_from_a_healthy_server(
        self, state
    ) -> None:
        """It is a read through to the server handle, and a reachable server
        has nothing to explain."""
        health = _health(AsyncMock(return_value=_reachable()), "stale failure")
        with patch(f"{STATE_MODULE}.graph_health", return_value=health):
            await state.refresh()

        assert state.error == ""
        health.startup_error.assert_not_called()

    async def test_loading_resets_even_when_the_read_raises(self, state) -> None:
        with (
            _patch_status_raising(RuntimeError),
            pytest.raises(RuntimeError),
        ):
            await state.refresh()

        assert state.loading is False


class TestComputedVars:
    def test_label_and_colour_before_the_first_check(self, state) -> None:
        assert state.status_label == "Checking…"
        assert state.status_color == "gray"

    def test_label_and_colour_when_connected(self, state) -> None:
        state.checked = True
        state.reachable = True

        assert state.status_label == "Connected"
        assert state.status_color == "green"

    def test_label_and_colour_when_unreachable(self, state) -> None:
        state.checked = True
        state.reachable = False

        assert state.status_label == "Unreachable"
        assert state.status_color == "red"

    def test_has_graphs_is_false_when_empty(self, state) -> None:
        assert state.has_graphs is False


class TestPolling:
    async def test_start_polling_sets_the_flag_and_hands_off(self, state) -> None:
        result = await state.start_polling()

        assert state.polling is True
        assert result is GraphStatusState.poll

    async def test_start_polling_is_a_no_op_when_already_running(self, state) -> None:
        state.polling = True

        assert await state.start_polling() is None

    async def test_stop_polling_clears_the_flag(self, state) -> None:
        state.polling = True

        state.stop_polling()

        assert state.polling is False

    async def test_poll_applies_each_reading_until_told_to_stop(self, state) -> None:
        state.polling = True
        state.poll_interval = 0
        read, calls = _scripted(state, _reachable())

        with _patched_loop(read):
            await _run_poll(state)

        assert state.reachable is True
        assert state.polling is False
        assert calls["n"] == 2  # one reading applied, one that stopped the loop

    async def test_poll_survives_a_failing_read_and_keeps_going(self, state) -> None:
        state.polling = True
        state.poll_interval = 0
        read, calls = _scripted(state, RuntimeError("transient"), _reachable())

        with _patched_loop(read):
            await _run_poll(state)

        assert calls["n"] == 3  # failure, success, stop
        assert state.reachable is True

    async def test_a_reading_that_arrives_after_stop_is_discarded(self, state) -> None:
        """Once polling is off the panel must not be mutated behind the scenes."""
        state.polling = True
        state.poll_interval = 0
        read, _ = _scripted(state)  # first call already stops the loop

        with _patched_loop(read):
            await _run_poll(state)

        assert state.checked is False
        assert state.reachable is False


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m 0s"),
        (125, "2m 5s"),
        (3600, "1h 0m"),
        (3725, "1h 2m"),
        (90000, "25h 0m"),
    ],
)
def test_format_uptime(seconds, expected) -> None:
    assert format_uptime(seconds) == expected


async def test_checked_at_is_converted_out_of_utc(state) -> None:
    """The domain stores UTC; the panel must show the reader's local time."""
    with _patch_status(_reachable()):
        await state.refresh()

    assert state.checked_at == CHECKED_AT.astimezone().strftime("%H:%M:%S")
    assert state.checked_at != CHECKED_AT.strftime("%H:%M:%S") or (
        CHECKED_AT.astimezone().utcoffset() == CHECKED_AT.utcoffset()
    )
