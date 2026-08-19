"""`GraphConfig` — what each backend needs named, and what it refuses."""

import pytest
from pydantic import ValidationError

from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphBackend, GraphServerMode


def _remote(backend: GraphBackend, **overrides) -> GraphConfig:
    return GraphConfig(mode=GraphServerMode.REMOTE, backend=backend, **overrides)


class TestDriverOptions:
    """Each backend names its target differently; the config translates."""

    def test_falkordb_is_addressed_by_graph(self) -> None:
        config = GraphConfig(host="127.0.0.1", port=6379, graph_name="mail-archive")

        assert config.driver_options() == {
            "host": "127.0.0.1",
            "port": 6379,
            "graph": "mail-archive",
        }

    def test_a_bolt_backend_is_addressed_by_database_and_credentials(self) -> None:
        config = _remote(
            GraphBackend.NEO4J,
            host="neo.internal",
            port=7687,
            database="archive",
            username="neo4j",
            password="secret",  # noqa: S106
        )

        assert config.driver_options() == {
            "host": "neo.internal",
            "port": 7687,
            "database": "archive",
            "username": "neo4j",
            "password": "secret",
        }

    def test_age_needs_both_a_database_and_a_graph(self) -> None:
        config = _remote(GraphBackend.AGE, port=5432, graph_name="mail-archive")

        options = config.driver_options()

        assert options["graph"] == "mail-archive"
        assert options["database"] == "mail-archive"

    def test_the_graph_name_stands_in_for_an_unset_database(self) -> None:
        """One setting names the thing being queried, whatever it is called."""
        config = _remote(GraphBackend.MEMGRAPH, graph_name="mail-archive")

        assert config.driver_options()["database"] == "mail-archive"

    def test_the_password_is_unwrapped_only_at_the_driver_boundary(self) -> None:
        """It is a `SecretStr` everywhere else, so it cannot be logged by accident."""
        config = _remote(GraphBackend.NEO4J, password="secret")  # noqa: S106

        assert "secret" not in repr(config)
        assert config.driver_options()["password"] == "secret"  # noqa: S105

    def test_a_missing_password_is_sent_as_empty_not_as_none(self) -> None:
        config = _remote(GraphBackend.NEO4J)

        assert config.driver_options()["password"] == ""


class TestLocalModeGuard:
    def test_local_mode_rejects_a_backend_it_cannot_start(self) -> None:
        """The vendored runtime is a redis-server; nothing else fits behind it."""
        with pytest.raises(ValidationError, match="mode=remote"):
            GraphConfig(mode=GraphServerMode.LOCAL, backend=GraphBackend.NEO4J)

    def test_local_mode_names_the_offending_backend(self) -> None:
        with pytest.raises(ValidationError, match="neo4j"):
            GraphConfig(mode=GraphServerMode.LOCAL, backend=GraphBackend.NEO4J)

    def test_falkordb_runs_locally(self) -> None:
        config = GraphConfig(mode=GraphServerMode.LOCAL, backend=GraphBackend.FALKORDB)

        assert config.mode is GraphServerMode.LOCAL

    def test_any_backend_may_be_reached_remotely(self) -> None:
        for backend in GraphBackend:
            assert _remote(backend).backend is backend


def test_the_endpoint_reads_as_host_and_port() -> None:
    assert _remote(GraphBackend.NEO4J, host="neo.internal", port=7687).endpoint == (
        "neo.internal:7687"
    )
