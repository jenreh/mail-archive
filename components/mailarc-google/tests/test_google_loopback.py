"""The loopback redirect server, driven with real sockets.

Why this exists at all: ``InstalledAppFlow.run_local_server`` accepts exactly one
TCP connection and reads it without a timeout. Browsers open speculative
"preconnect" sockets before they send the redirect, and when the server picks
one of those it blocks forever while the real redirect sits unanswered in the
backlog — the consent looks abandoned although the user finished it. Seen in
the wild on 2026-08-21; reproduced below as the first test.

Nothing here opens a browser: the tests *are* the browser, one socket at a time.
"""

import socket
import threading
import time

import pytest

from mailarc_core.mail.errors import MailAuthError
from mailarc_google.source.loopback import (
    LoopbackServer,
    RedirectDenied,
    RedirectTimeout,
)

LOOPBACK = "127.0.0.1"


def _request(port: int, path: str) -> bytes:
    """One HTTP/1.1 GET the way a browser sends it; returns the whole response.

    Read until the server closes (it sends ``Connection: close``): headers and
    body may well arrive in separate segments.
    """
    with socket.create_connection((LOOPBACK, port), timeout=3) as sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        chunks = []
        while chunk := sock.recv(4096):
            chunks.append(chunk)
        return b"".join(chunks)


class TestItSurvivesWhatBrowsersDo:
    def test_an_idle_preconnect_does_not_starve_the_real_redirect(self) -> None:
        """The bug that made a finished consent look abandoned."""
        with LoopbackServer(LOOPBACK) as server:
            idle = socket.create_connection((LOOPBACK, server.port))
            time.sleep(0.1)  # let the server accept the silent socket first
            try:
                head = _request(server.port, "/?state=s&code=abc")
                path = server.wait(timeout_s=3)
            finally:
                idle.close()

        assert head.startswith(b"HTTP/1.0 200")
        assert path == "/?state=s&code=abc"

    def test_a_favicon_probe_is_answered_and_ignored(self) -> None:
        with LoopbackServer(LOOPBACK) as server:
            favicon = _request(server.port, "/favicon.ico")
            _request(server.port, "/?state=s&code=abc")
            path = server.wait(timeout_s=3)

        assert favicon.startswith(b"HTTP/1.0 404")
        assert path.endswith("code=abc")

    def test_only_the_first_redirect_counts(self) -> None:
        """A reloaded success page must not overwrite the code that was used."""
        with LoopbackServer(LOOPBACK) as server:
            _request(server.port, "/?state=s&code=first")
            _request(server.port, "/?state=s&code=second")
            path = server.wait(timeout_s=3)

        assert path.endswith("code=first")

    def test_the_success_page_tells_the_user_to_close_the_tab(self) -> None:
        with LoopbackServer(LOOPBACK) as server:
            head = _request(server.port, "/?code=abc&state=s")
            server.wait(timeout_s=3)

        assert b"text/html" in head
        assert b"close this window" in head


class TestWhenNobodyComesBack:
    def test_it_gives_up_after_the_deadline(self) -> None:
        started = time.monotonic()
        with LoopbackServer(LOOPBACK) as server, pytest.raises(RedirectTimeout):
            server.wait(timeout_s=0.2)

        assert time.monotonic() - started < 2

    def test_google_saying_no_is_a_denial_not_a_timeout(self) -> None:
        with LoopbackServer(LOOPBACK) as server:
            _request(server.port, "/?error=access_denied&state=s")
            with pytest.raises(RedirectDenied, match="access_denied"):
                server.wait(timeout_s=3)

    def test_leaving_the_block_stops_the_server_and_its_threads(self) -> None:
        before = threading.active_count()
        with LoopbackServer(LOOPBACK) as server:
            port = server.port
            _request(server.port, "/?code=abc&state=s")
            server.wait(timeout_s=3)

        with pytest.raises(OSError):
            socket.create_connection((LOOPBACK, port), timeout=1)
        assert threading.active_count() <= before + 1, "no lingering handler threads"


class TestTheRedirectUri:
    def test_it_names_the_port_the_server_actually_bound(self) -> None:
        with LoopbackServer(LOOPBACK) as server:
            assert server.redirect_uri == f"http://{LOOPBACK}:{server.port}/"
            assert server.port != 0

    def test_a_busy_port_is_an_auth_error_the_page_can_show(self) -> None:
        with (
            LoopbackServer(LOOPBACK) as first,
            pytest.raises(MailAuthError, match="in use"),
        ):
            LoopbackServer(LOOPBACK, port=first.port).__enter__()
