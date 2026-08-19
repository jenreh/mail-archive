"""The backend-independent seam: drivers and sessions, whoever answers.

The `Fake*` drivers here implement :class:`runic.ogm.GraphDriver` and nothing
else — no FalkorDB, no redis, not even an import of one. That they work is the
claim this module makes: `client` talks to runic's protocol, not to a vendor.
"""

import pytest
from runic.ogm import GraphDialect, GraphDriver, Neo4jDialect, Session

from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphBackend, GraphServerMode


class FakeResult:
    def __init__(self, rows: list[list[int]]):
        self.rows = rows
        self.columns = ["value"]


class FakeDriver:
    """A minimal `GraphDriver`: dialect, execute, close. Nothing vendor-shaped."""

    def __init__(self) -> None:
        self.closed = False
        self.executed: list[str] = []

    @property
    def dialect(self) -> GraphDialect:
        # Any real dialect will do — none of these tests generate Cypher.
        return Neo4jDialect()

    def execute(self, cypher: str, params: dict) -> FakeResult:  # noqa: ARG002
        self.executed.append(cypher)
        return FakeResult([[1]])

    def close(self) -> None:
        self.closed = True


class TransactionalDriver(FakeDriver):
    """Adds begin/commit/rollback, so runic sees a `TransactionalGraphDriver`."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def begin(self) -> None:
        self.events.append("begin")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


@pytest.fixture
def config() -> GraphConfig:
    return GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="graph.internal",
        port=6380,
        graph_name="mail-archive",
    )


@pytest.fixture
def remote_neo4j() -> GraphConfig:
    return GraphConfig(
        mode=GraphServerMode.REMOTE,
        backend=GraphBackend.NEO4J,
        host="neo.internal",
        port=7687,
        graph_name="mail-archive",
        username="neo4j",
        password="secret",  # noqa: S106
    )


def _driver_factory(driver: GraphDriver, seen: dict):
    def build(backend: str, **kwargs) -> GraphDriver:
        seen["backend"] = backend
        seen["kwargs"] = kwargs
        return driver

    return build


class TestConnect:
    def test_falkordb_is_addressed_by_graph(self, config, monkeypatch) -> None:
        seen: dict = {}
        monkeypatch.setattr(
            client, "create_driver", _driver_factory(FakeDriver(), seen)
        )

        client.connect(config)

        assert seen["backend"] == "falkordb"
        assert seen["kwargs"] == {
            "host": "graph.internal",
            "port": 6380,
            "graph": "mail-archive",
        }

    def test_a_bolt_backend_is_addressed_by_database_and_credentials(
        self, remote_neo4j, monkeypatch
    ) -> None:
        """The same config field names a graph or a database, per backend."""
        seen: dict = {}
        monkeypatch.setattr(
            client, "create_driver", _driver_factory(FakeDriver(), seen)
        )

        client.connect(remote_neo4j)

        assert seen["backend"] == "neo4j"
        assert seen["kwargs"] == {
            "host": "neo.internal",
            "port": 7687,
            "database": "mail-archive",
            "username": "neo4j",
            "password": "secret",
        }


class TestClose:
    def test_a_driver_that_closes_itself_is_simply_closed(self) -> None:
        """Every backend but FalkorDB releases its own connection."""
        driver = FakeDriver()

        client.close(driver)

        assert driver.closed is True


class TestSession:
    def test_yields_a_runic_session_over_the_configured_driver(
        self, config, monkeypatch
    ) -> None:
        driver = FakeDriver()
        monkeypatch.setattr(client, "create_driver", _driver_factory(driver, {}))

        with client.session(config) as graph_session:
            assert isinstance(graph_session, Session)
            assert driver.closed is False

        assert driver.closed is True

    def test_works_against_a_backend_with_no_falkordb_anywhere_in_it(
        self, remote_neo4j, monkeypatch
    ) -> None:
        """The point of the protocol: nothing here knows what a redis is."""
        driver = FakeDriver()
        monkeypatch.setattr(client, "create_driver", _driver_factory(driver, {}))

        with client.session(remote_neo4j) as graph_session:
            graph_session.execute("MATCH (n) RETURN n", {})

        assert driver.executed == ["MATCH (n) RETURN n"]
        assert driver.closed is True

    def test_a_transactional_backend_gets_a_real_transaction(
        self, remote_neo4j, monkeypatch
    ) -> None:
        """runic opens one itself once the driver satisfies the protocol."""
        driver = TransactionalDriver()
        monkeypatch.setattr(client, "create_driver", _driver_factory(driver, {}))

        with client.session(remote_neo4j) as graph_session:
            graph_session.execute("MATCH (n) RETURN n", {})

        assert driver.events == ["begin", "commit"]

    def test_a_transactional_backend_rolls_back_when_the_body_raises(
        self, remote_neo4j, monkeypatch
    ) -> None:
        driver = TransactionalDriver()
        monkeypatch.setattr(client, "create_driver", _driver_factory(driver, {}))

        with (
            pytest.raises(RuntimeError, match="boom"),
            client.session(remote_neo4j) as graph_session,
        ):
            graph_session.execute("MATCH (n) RETURN n", {})
            raise RuntimeError("boom")

        assert driver.events == ["begin", "rollback"]

    def test_the_driver_is_closed_when_the_body_raises(
        self, config, monkeypatch
    ) -> None:
        """A failed unit of work must not leak the connection it borrowed."""
        driver = FakeDriver()
        monkeypatch.setattr(client, "create_driver", _driver_factory(driver, {}))

        with pytest.raises(RuntimeError, match="boom"), client.session(config):
            raise RuntimeError("boom")

        assert driver.closed is True
