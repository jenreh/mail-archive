"""The FalkorDB-only corner: PING, the raw handle, and the close() quirk.

Every function here refuses to work against another backend, and the tests
say so — that refusal is what keeps :mod:`~mailarc_core.graph.client` honest
about being backend-independent.
"""

import contextlib
import socket
import threading
from typing import Any, Never

import pytest
from runic.ogm import FalkorDBDriver, GraphDriver

from mailarc_core.graph import admin

_PROBE_PATIENCE_SECONDS = 15
"""How long a probe of a silent host may take before the test calls it hung.

Generous against ``admin._PING_TIMEOUT_SECONDS`` of one, because the number
being pinned is "bounded at all" and not "bounded at one second" — a CI box
under load must not turn a fixed bug red. The failure it catches is unbounded,
so any finite ceiling separates the two.
"""


class FakeGraph:
    def __init__(self, name: str):
        self.name = name

    def query(self, cypher: str, params=None) -> Never:  # noqa: ARG002
        raise AssertionError("no query expected in these tests")


class FakeDB:
    def __init__(self, fail_close: bool = False):
        self.selected: list[str] = []
        self.closed = False
        self.graphs: list[str] = []
        self._fail_close = fail_close

    def select_graph(self, name: str) -> FakeGraph:
        self.selected.append(name)
        return FakeGraph(name)

    def list_graphs(self) -> list[str]:
        return self.graphs

    def close(self) -> None:
        if self._fail_close:
            raise RuntimeError("connection already gone")
        self.closed = True


class NotFalkorDB:
    """A bare :class:`runic.ogm.GraphDriver` — the shape any backend presents."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def dialect(self) -> Any:
        raise AssertionError("dialect not needed here")

    def execute(self, cypher: str, params: dict) -> Never:  # noqa: ARG002
        raise AssertionError("no query expected in these tests")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def driver(db: FakeDB) -> GraphDriver:
    return FalkorDBDriver(db.select_graph("mail-archive"), db)


class TestRelease:
    def test_the_connection_under_a_falkordb_driver_is_closed(self, driver, db) -> None:
        """`FalkorDBDriver.close` is a no-op — the socket has to be closed here."""
        admin.release(driver)

        assert db.closed is True

    def test_another_backend_is_left_alone(self) -> None:
        """Every other runic driver closes its own connection in `close()`."""
        other = NotFalkorDB()

        admin.release(other)

        assert other.closed is False

    def test_a_handle_that_refuses_to_close_is_not_an_error(self) -> None:
        """A status poll must not fail because teardown did."""
        broken = FakeDB(fail_close=True)

        admin.release(FalkorDBDriver(broken.select_graph("g"), broken))

        assert broken.closed is False


class TestConnection:
    def test_hands_back_the_raw_handle(self, driver, db) -> None:
        assert admin.connection(driver) is db

    def test_another_backend_is_refused_by_name(self) -> None:
        """The error has to name the fix, not just the symptom."""
        with pytest.raises(TypeError, match=r"config\.backend"):
            admin.connection(NotFalkorDB())


class TestGraphNames:
    def test_lists_what_the_server_holds(self, driver, db) -> None:
        db.graphs = ["mail-archive", "scratch"]

        assert admin.graph_names(driver) == ("mail-archive", "scratch")

    def test_an_empty_server_lists_nothing(self, driver) -> None:
        assert admin.graph_names(driver) == ()


class TestDriverFor:
    def test_binds_another_graph_on_the_same_connection(self, driver, db) -> None:
        sibling = admin.driver_for(driver, "scratch")

        assert admin.connection(sibling) is db
        assert db.selected[-1] == "scratch"

    def test_the_original_driver_is_left_alone(self, driver) -> None:
        _, before = driver.falkordb_connection()

        admin.driver_for(driver, "scratch")

        _, after = driver.falkordb_connection()
        assert after is before


class TestIsServing:
    def test_a_server_that_answers_ping_is_serving(self, monkeypatch) -> None:
        class Answering:
            def __init__(self, **kwargs) -> None:
                self.connection = self

            def ping(self) -> bool:
                return True

            def close(self) -> None:
                pass

        monkeypatch.setattr(admin, "FalkorDB", Answering)

        assert admin.is_serving("127.0.0.1", 6379) is True

    def test_a_refused_connection_is_not_serving(self, monkeypatch) -> None:
        def refuse(**kwargs) -> Never:
            raise ConnectionError("refused")

        monkeypatch.setattr(admin, "FalkorDB", refuse)

        assert admin.is_serving("127.0.0.1", 6379) is False

    def test_a_server_that_fails_the_ping_is_not_serving(self, monkeypatch) -> None:
        class Mute:
            def __init__(self, **kwargs) -> None:
                self.connection = self

            def ping(self) -> Never:
                raise TimeoutError("no reply")

            def close(self) -> None:
                pass

        monkeypatch.setattr(admin, "FalkorDB", Mute)

        assert admin.is_serving("127.0.0.1", 6379) is False

    def test_a_host_that_accepts_and_then_says_nothing_still_answers(self) -> None:
        """The one case every other test here fakes, against a real socket.

        The three above monkeypatch ``FalkorDB``, so they assert what this
        module does with an exception rather than whether one ever arrives —
        and the answer was that it did not. A host that completes the TCP
        handshake and then sends nothing (a VPN, a corporate firewall, a
        draining load balancer, a wedged server) left the PING blocked
        forever, because only ``socket_connect_timeout`` was set and it does
        not bound a read.

        That is not a slow probe, it is one that never returns, and
        ``FalkorDBServer._await_ready`` checks its ``startup_timeout``
        *between* probes — so the desktop application hung at startup with
        nothing in the log. Observed on a network that accepts connections to
        unrouted addresses, which is what made
        ``test_server_local.py::test_a_server_that_dies_at_startup_reports_its_output``
        hang rather than fail.

        Run on a thread with a join, so a regression is a red test rather than
        a suite that stops here.
        """
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        accepted: list[socket.socket] = []

        def accept_and_ignore() -> None:
            with contextlib.suppress(OSError):
                connection, _ = listener.accept()
                accepted.append(connection)

        door = threading.Thread(target=accept_and_ignore, daemon=True)
        door.start()

        answer: list[bool] = []
        probe = threading.Thread(
            target=lambda: answer.append(
                admin.is_serving("127.0.0.1", listener.getsockname()[1])
            ),
            daemon=True,
        )
        probe.start()
        probe.join(timeout=_PROBE_PATIENCE_SECONDS)

        try:
            assert not probe.is_alive(), (
                "is_serving never returned against a host that accepts and "
                "stays silent — it needs a read timeout, not only a connect one"
            )
            assert answer == [False]
        finally:
            for connection in accepted:
                connection.close()
            listener.close()
