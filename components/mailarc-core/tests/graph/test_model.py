from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from mailarc_core.graph.model import (
    GraphInfo,
    GraphServerMode,
    GraphServerStatus,
    ServerMetrics,
)

CHECKED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _status(**overrides: Any) -> GraphServerStatus:
    defaults: dict[str, Any] = {
        "mode": GraphServerMode.LOCAL,
        "endpoint": "127.0.0.1:6379",
        "reachable": True,
        "checked_at": CHECKED_AT,
    }
    return GraphServerStatus(**{**defaults, **overrides})


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("4.20.3", True),
        ("4.0.0", True),
        ("5.1.0", True),
        ("3.9.9", False),
        ("2.12.0", False),
        (None, False),
        ("", False),
        ("not-a-version", False),
    ],
)
def test_vector_knn_supported_tracks_major_version(version, expected) -> None:
    assert _status(falkordb_version=version).vector_knn_supported is expected


def test_unreachable_factory_records_the_error_and_clears_details() -> None:
    status = GraphServerStatus.unreachable(
        mode=GraphServerMode.REMOTE,
        endpoint="graph.internal:6379",
        checked_at=CHECKED_AT,
        error="ConnectionError: refused",
    )

    assert status.reachable is False
    assert status.error == "ConnectionError: refused"
    assert status.mode is GraphServerMode.REMOTE
    assert status.endpoint == "graph.internal:6379"
    assert status.graphs == ()
    assert status.metrics is None
    assert status.vector_knn_supported is False


def test_status_is_immutable() -> None:
    status = _status()
    with pytest.raises(ValidationError):
        status.reachable = False  # ty: ignore[invalid-assignment]


def test_a_bad_field_value_is_rejected_on_construction() -> None:
    """The reason these are pydantic models: garbage never gets in."""
    with pytest.raises(ValidationError):
        _status(mode="not-a-mode")


def test_status_carries_graphs_and_metrics() -> None:
    status = _status(
        graphs=(GraphInfo(name="mail-archive", node_count=3, edge_count=2),),
        metrics=ServerMetrics(
            uptime_seconds=42,
            used_memory_human="1.2M",
            connected_clients=1,
            total_commands_processed=17,
        ),
    )

    assert status.graphs[0].name == "mail-archive"
    assert status.graphs[0].node_count == 3
    assert status.metrics is not None
    assert status.metrics.used_memory_human == "1.2M"


def test_mode_is_a_string_enum_so_it_survives_serialisation() -> None:
    assert GraphServerMode.LOCAL == "local"
    assert GraphServerMode.REMOTE == "remote"
