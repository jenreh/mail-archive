"""The throwaway HTTP server that catches Google's redirect on a desktop.

Google's installed-app flow ends with the browser being sent to a loopback URL
the program is listening on. google-auth-oauthlib ships one such listener
(``run_local_server``), and it is unusable in practice: it accepts exactly one
TCP connection and reads it without a timeout. Browsers open speculative
"preconnect" sockets before they send anything, and whenever the listener
picks one of those it blocks forever while the real redirect waits unanswered
in the backlog — a finished consent looks abandoned, at random.

So this is the listener instead. It accepts any number of connections, drops
a silent one after a few seconds, answers probes such as ``/favicon.ico`` with
a 404, and finishes on the first request that carries ``code`` or ``error``.
It knows nothing about OAuth beyond those two parameter names; the exchange
of the code is :mod:`mailarc_google.source.oauth`'s job.
"""

import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import parse_qs, urlparse

from mailarc_core.mail.errors import MailAuthError

logger = logging.getLogger(__name__)

CONNECTION_TIMEOUT_S = 5.0
"""Seconds a single connection may stay silent before it is dropped.

This is what makes a browser's idle preconnect harmless: the handler thread
waiting on it gives up, and the thread serving the real redirect was never
blocked by it in the first place.
"""

SUCCESS_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>Login received</title>"
    "<p style='font:16px system-ui'>Login received. You may close this window.</p>"
)


class RedirectTimeout(Exception):
    """Nobody came back to the loopback address within the deadline."""


class RedirectDenied(Exception):
    """Google came back with an ``error`` parameter instead of a code."""


class LoopbackServer:
    """Listens on a loopback port for one redirect; a context manager.

    ``port=0`` lets the operating system pick a free one, which is what a
    desktop application wants and what Google permits for installed-app
    clients. Entering the block binds and starts serving on a thread; leaving
    it stops the server whatever happened, so an abandoned consent cannot
    leave a listener behind.
    """

    def __init__(self, host: str, port: int = 0) -> None:
        self._host = host
        self._port = port
        self._server: _RedirectServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        try:
            self._server = _RedirectServer(self._host, self._port)
        except OSError as error:
            raise MailAuthError(
                f"loopback port {self._port} is already in use: {error}"
            ) from error
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="gmail-consent-redirect",
            daemon=True,
        )
        self._thread.start()
        logger.debug("Listening for the consent redirect on %s", self.redirect_uri)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=CONNECTION_TIMEOUT_S)

    @property
    def port(self) -> int:
        return self._running().server_port

    @property
    def redirect_uri(self) -> str:
        """What the authorization request names — with the port actually bound."""
        return f"http://{self._host}:{self.port}/"

    def wait(self, timeout_s: float) -> str:
        """Block until the redirect arrives; return its path and query.

        Raises :class:`RedirectTimeout` when the deadline passes and
        :class:`RedirectDenied` when Google answered with an error — the user
        pressed Cancel, or the account was not allowed to grant.
        """
        server = self._running()
        if not server.received.wait(timeout_s):
            raise RedirectTimeout(f"no redirect within {timeout_s:.0f}s")
        path = server.callback_path or "/"
        query = parse_qs(urlparse(path).query)
        if "error" in query:
            raise RedirectDenied(query["error"][0])
        return path

    def _running(self) -> _RedirectServer:
        if self._server is None:
            raise RuntimeError("LoopbackServer is used as a context manager")
        return self._server


class _RedirectServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # a busy port must fail loudly, not share

    def __init__(self, host: str, port: int) -> None:
        super().__init__((host, port), _RedirectHandler)
        self.received = threading.Event()
        self.callback_path: str | None = None


class _RedirectHandler(BaseHTTPRequestHandler):
    timeout = CONNECTION_TIMEOUT_S

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        server = cast(_RedirectServer, self.server)
        query = parse_qs(urlparse(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = SUCCESS_PAGE.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        if not server.received.is_set():
            server.callback_path = self.path
            server.received.set()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The default writes to stderr; the path carries the authorization
        # code, so it stays at debug and out of any log a user would share.
        logger.debug("redirect server: %s", format % args)
