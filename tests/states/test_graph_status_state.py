from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.states.graph_status_state import GraphStatusState, format_uptime
from mailarc_core import (
    GraphInfo,
    GraphServerMode,
    GraphServerStatus,
    ServerMetrics,
)


async def _run_poll(state: GraphStatusState) -> None:
    """Invoke the background handler's underlying coroutine.

    Reflex refuses a direct `state.poll()` call on a background handler, so go
    through the EventHandler's wrapped function.
    """
    await GraphStatusState.poll.fn(state)


CHECKED_AT = datetime(2026, 8, 18, 14, 30, 5, tzinfo=UTC)

STATE_MODULE = "app.states.graph_status_state"


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


@pytest.fixture
def state() -> GraphStatusState:
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


def _patched_loop(read):
    """Patch the collaborators plus the state lock the background task takes."""
    return _MultiPatch(
        patch(f"{STATE_MODULE}.graph_status", read),
        patch(f"{STATE_MODULE}.graph_startup_error", return_value=None),
        patch.object(GraphStatusState, "__aenter__", AsyncMock()),
        patch.object(GraphStatusState, "__aexit__", AsyncMock(return_value=False)),
    )


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


def _patch_status(status: GraphServerStatus):
    return patch(f"{STATE_MODULE}.graph_status", AsyncMock(return_value=status))


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

        with (
            _patch_status(unreachable),
            patch(f"{STATE_MODULE}.graph_startup_error", return_value=None),
        ):
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

        with (
            _patch_status(unreachable),
            patch(
                f"{STATE_MODULE}.graph_startup_error",
                return_value="FalkorDB runtime incomplete — run `task tauri:vendor`",
            ),
        ):
            await state.refresh()

        assert "task tauri:vendor" in state.error

    async def test_loading_resets_even_when_the_read_raises(self, state) -> None:
        with (
            patch(f"{STATE_MODULE}.graph_status", AsyncMock(side_effect=RuntimeError)),
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
