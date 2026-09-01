"""The listener, and the one failure it exists to prevent.

A one-shot listener that accepts a single connection and reads it without a
timeout looks correct and is not: browsers open speculative preconnect sockets
before they send anything, so it regularly blocks forever on an empty
connection while the real redirect waits unanswered in the backlog. The
consent then looks abandoned, at random, and nothing in a log says why.

`test_a_silent_preconnect_does_not_block_the_real_redirect` is that scenario
written down.
"""

import socket
import threading
import urllib.error
import urllib.request

import pytest

from mailarc_core.mail.errors import MailAuthError
from mailarc_m365.source.loopback import (
    LoopbackServer,
    RedirectDenied,
    RedirectTimeout,
)

HOST = "localhost"


def visit(url: str, timeout: float = 5.0) -> int:
    """What a browser does with the redirect, reduced to its status code."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as answer:  # noqa: S310
            answer.read()
            return int(answer.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


class TestTheRedirect:
    def test_the_uri_names_the_port_the_system_actually_handed_out(self) -> None:
        with LoopbackServer(HOST, 0) as server:
            assert server.port > 0
            assert server.redirect_uri == f"http://{HOST}:{server.port}/"

    def test_a_code_finishes_the_wait_and_comes_back_flat(self) -> None:
        with LoopbackServer(HOST, 0) as server:
            assert visit(f"{server.redirect_uri}?code=THE-CODE&state=st") == 200
            assert server.wait(2.0) == {"code": "THE-CODE", "state": "st"}

    def test_an_error_is_a_denial_carrying_what_entra_said(self) -> None:
        with LoopbackServer(HOST, 0) as server:
            visit(f"{server.redirect_uri}?error=access_denied&error_description=nope")
            with pytest.raises(RedirectDenied) as raised:
                server.wait(2.0)

        assert raised.value.error == "access_denied"
        assert "nope" in str(raised.value)

    def test_nobody_coming_back_is_a_timeout_and_not_a_hang(self) -> None:
        with LoopbackServer(HOST, 0) as server, pytest.raises(RedirectTimeout):
            server.wait(0.2)

    def test_a_probe_is_answered_and_ignored(self) -> None:
        with LoopbackServer(HOST, 0) as server:
            assert visit(f"{server.redirect_uri}favicon.ico") == 404
            with pytest.raises(RedirectTimeout):
                server.wait(0.2)

    def test_a_silent_preconnect_does_not_block_the_real_redirect(self) -> None:
        """The whole reason this file exists rather than a one-shot accept."""
        with LoopbackServer(HOST, 0) as server:
            preconnects = [
                socket.create_connection((HOST, server.port)) for _ in range(3)
            ]
            try:
                caught: list[int] = []
                visitor = threading.Thread(
                    target=lambda: caught.append(
                        visit(f"{server.redirect_uri}?code=THE-CODE")
                    ),
                    daemon=True,
                )
                visitor.start()
                assert server.wait(5.0)["code"] == "THE-CODE"
                visitor.join(timeout=5.0)
                assert caught == [200]
            finally:
                for one in preconnects:
                    one.close()

    def test_the_first_redirect_wins_and_a_second_changes_nothing(self) -> None:
        with LoopbackServer(HOST, 0) as server:
            visit(f"{server.redirect_uri}?code=FIRST")
            visit(f"{server.redirect_uri}?code=SECOND")
            assert server.wait(2.0)["code"] == "FIRST"


class TestTheLifecycle:
    def test_a_busy_port_fails_loudly_rather_than_sharing(self) -> None:
        with (
            LoopbackServer(HOST, 0) as taken,
            pytest.raises(MailAuthError, match="already in use"),
            LoopbackServer(HOST, taken.port),
        ):
            pass

    def test_leaving_the_block_stops_listening(self) -> None:
        with LoopbackServer(HOST, 0) as server:
            port = server.port
        with pytest.raises(OSError):
            socket.create_connection((HOST, port), timeout=1.0).close()

    def test_using_it_without_the_block_says_so(self) -> None:
        with pytest.raises(RuntimeError, match="context manager"):
            LoopbackServer(HOST, 0).wait(0.1)
