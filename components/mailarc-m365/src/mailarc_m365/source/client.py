"""The HTTP half of the adapter, and the only place that reads a status code.

Three jobs and no fourth: put a live access token on every request, turn what
Graph answers into the error taxonomy, and close the connection pool. What the
JSON *means* is :mod:`~mailarc_m365.source.mapping`'s business; which call to
make is :mod:`~mailarc_m365.source.source`'s.

**No ``httpx`` exception leaves this module**, and reading that as "catch
``RequestError``" is how one gets out: ``httpx.InvalidURL`` descends from
``Exception`` and ``httpx.StreamError`` from ``RuntimeError``, neither of them
from ``RequestError``, and encoding a URL can raise ``UnicodeEncodeError`` from
inside the library itself. An adapter that lets any of them through has not
decided whether the engine should retry, and the engine has no way to decide
for it. The same goes for MSAL, whose errors are already mapped one layer down
in :mod:`~mailarc_m365.source.credentials`.

**It does not retry either.** The engine already backs off with jitter and
honours ``Retry-After``; a second loop underneath it would multiply every wait
by a number invisible from the outside. The single exception is a 401, which
usually means the access token aged out mid-run: the client refreshes **once**
and repeats the call. A second 401 is a credential the user has to grant again,
not a clock.

**Two statuses have no meaning without the question.** A 404 says "this message
is gone" when a message was asked for and "that folder does not exist" when a
folder was; a 410 says "this delta token is too old" on the delta and something
else anywhere else. Those are different instructions — skip one message, or
throw the cursor away and walk the whole mailbox — and the status alone cannot
tell them apart. So :meth:`GraphClient.get_json` takes a ``not_found`` and a
``gone`` type and the call site names which question it asked. The decision
still happens here, in the one module allowed to read a status code; only the
*meaning* travels in, and only for those two.
"""

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_m365.source.config import M365Config
from mailarc_m365.source.credentials import M365Credentials, refresh_async
from mailarc_m365.source.model import retry_after_seconds

logger = logging.getLogger(__name__)

_SERVER_ERROR = 500
"""From here up Graph is broken, not the request."""

_GONE = 410
"""``httpx.codes.GONE`` by number, so the comparison reads like the others."""


class GraphApiError(BaseModel):
    """Graph's refusal, reduced to the two parts worth reading.

    The *decision* comes off the status code — a code is stable and Microsoft's
    ``message`` is localised and not. ``code`` is kept because it is the one
    string an operator needs to search for: ``ErrorItemNotFound``,
    ``resyncRequired``, ``MailboxNotEnabledForRESTAPI``.
    """

    model_config = ConfigDict(frozen=True)

    code: str = ""
    message: str = ""

    @classmethod
    def read(cls, response: httpx.Response) -> GraphApiError:
        """Dig the envelope out of a response, or give up quietly.

        A refusal that carries no JSON at all — a proxy, a captive portal, an
        HTML error page — is still a refusal, and the status code already said
        what to do about it. So nothing here raises.
        """
        try:
            body = response.json()
        except ValueError:
            return cls()
        envelope = body.get("error") if isinstance(body, dict) else None
        if not isinstance(envelope, dict):
            return cls()
        return cls(
            code=str(envelope.get("code") or ""),
            message=str(envelope.get("message") or ""),
        )

    def describe(self) -> str:
        """Microsoft's own words about the refusal, or an empty string."""
        return " ".join(part for part in (self.code, self.message) if part)


class GraphClient:
    """Authenticated GETs against one mailbox's Microsoft Graph.

    It holds the credentials because it is the only thing that can notice they
    went stale. They are frozen, so a refresh makes a *new* object and
    :attr:`credentials` is the only copy of it — ``app/worker.py`` wants a
    rotated refresh token back in ``mail_credentials``, and this attribute is
    where the owner reads it from.
    """

    def __init__(
        self, credentials: M365Credentials, config: M365Config | None = None
    ) -> None:
        self._config = config or M365Config()
        self._credentials = credentials
        self._http = httpx.AsyncClient(timeout=self._config.request_timeout)
        self._closed = False

    @property
    def credentials(self) -> M365Credentials:
        """The credentials as they stand now, refreshes included."""
        return self._credentials

    async def get_json(
        self,
        target: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        not_found: type[MailError] = MailPermanentError,
        gone: type[MailError] = MailPermanentError,
    ) -> dict[str, Any]:
        """One GET that answers with JSON, with the taxonomy already applied.

        ``target`` is either a path below the API root or a whole URL Graph
        itself handed out — a ``nextLink`` or a ``deltaLink``. Both go through
        :meth:`_url`, which is also where a URL pointing anywhere but the
        configured Graph origin is refused rather than sent a bearer token.

        Returns the decoded JSON object. Everything else — a refusal, a broken
        connection, a body that is not JSON — leaves as one of the four errors,
        so no caller has to know what ``httpx`` is.
        """
        response = await self._send(target, params, headers)
        if response.status_code == httpx.codes.UNAUTHORIZED:
            await self._refresh()
            response = await self._send(target, params, headers)
        if response.status_code != httpx.codes.OK:
            raise self._refusal(target, response, not_found, gone)
        return self._payload(target, response)

    async def get_bytes(
        self,
        target: str,
        *,
        not_found: type[MailError] = MailPermanentError,
    ) -> bytes:
        """One GET that answers with a body, not with JSON.

        Only ``/messages/{id}/$value`` needs this, and it needs it for the
        reason the whole design rests on: what goes into the archive is the RFC
        5322 bytes exactly as they were sent, because those bytes are what get
        hashed for ``eml_sha256`` and what go to the blob store. Decoding them
        into anything on the way through would make a later parser fix
        unreplayable.

        An empty body is a message Graph will never hand over — a draft with no
        MIME representation, a record left behind by a migration — so it is a
        skipped message rather than an empty one written to the archive.
        """
        response = await self._send(target, None, None)
        if response.status_code == httpx.codes.UNAUTHORIZED:
            await self._refresh()
            response = await self._send(target, None, None)
        if response.status_code != httpx.codes.OK:
            raise self._refusal(target, response, not_found, MailPermanentError)
        if not response.content:
            raise MailPermanentError(f"Graph answered {target} with an empty body")
        return response.content

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()

    async def _send(
        self,
        target: str,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        """One request, with a token that is alive when it goes out.

        The pre-flight refresh is what keeps the 401 path rare: a token that
        expires between two pages of a long import would otherwise cost a
        wasted round trip per call for the rest of the batch.
        """
        if self._credentials.needs_refresh():
            await self._refresh()
        sent = {"Authorization": self._credentials.authorization_header()}
        sent.update(headers or {})
        try:
            # `None` and not an empty dict: httpx *replaces* a URL's query with
            # whatever `params` holds, so `{}` would strip the `$skiptoken` off
            # every link Graph handed out and silently restart the enumeration.
            return await self._http.get(
                self._url(target),
                params=dict(params) if params else None,
                headers=sent,
            )
        except httpx.TimeoutException as error:
            raise MailTransientError(f"Graph timed out on {target}: {error}") from error
        except httpx.RequestError as error:
            raise MailTransientError(
                f"Graph is unreachable for {target}: {error}"
            ) from error
        except (httpx.InvalidURL, httpx.StreamError, UnicodeError, ValueError) as error:
            # Not every httpx failure is a `RequestError`: `InvalidURL` and
            # `StreamError` descend from `Exception` and `RuntimeError`, and
            # building the request encodes the URL, so a message id carrying a
            # lone surrogate — `json.loads` produces them from `\udcff` — comes
            # back as a `UnicodeEncodeError` from inside the library. Each of
            # them is an address this client cannot even form, so asking again
            # forms the same one: it is one skipped message, not a retry.
            raise MailPermanentError(
                f"Graph cannot be asked for {target}: {error}"
            ) from error
        except httpx.HTTPError as error:
            # The backstop for whatever httpx grows next. It is under
            # `HTTPError`, which means it is about this exchange, so the engine
            # is told to try the exchange again.
            raise MailTransientError(
                f"Graph could not be reached for {target}: {error}"
            ) from error

    async def _refresh(self) -> None:
        """Mint a new access token and keep whatever came back with it.

        Raises out of :func:`~mailarc_m365.source.credentials.refresh_async`
        unchanged: a rejected refresh token is terminal and an unreachable
        token endpoint is worth trying again, and both decisions were made
        there.
        """
        previous = getattr(self._credentials, "refresh_token", None)
        self._credentials = await refresh_async(
            self._credentials,
            client_id=self._config.client_id,
            client_secret=self._config.app_client_secret(),
            authority=self._config.authority_for(self._credentials.tenant_id),
        )
        if getattr(self._credentials, "refresh_token", None) != previous:
            logger.info(
                "Microsoft rotated the refresh token of an account — it has to "
                "be written back to mail_credentials"
            )

    def _refusal(
        self,
        target: str,
        response: httpx.Response,
        not_found: type[MailError],
        gone: type[MailError],
    ) -> MailError:
        """Turn a non-200 into the error that says what to do about it."""
        status = response.status_code
        described = GraphApiError.read(response)
        detail = f" — {described.describe()}" if described.describe() else ""
        if status == httpx.codes.TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
            # Graph documents both: 429 with a Retry-After for throttling, and
            # 503/504 during a service event, with the same instruction.
            return MailTransientError(
                f"Graph returned {status} for {target}{detail}",
                retry_after=retry_after_seconds(response.headers.get("Retry-After")),
            )
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            # Unlike Gmail, Graph does not spend its quota as a 403: throttling
            # is a 429 with a Retry-After, so a 403 here really is a permission
            # the grant does not carry.
            return MailAuthError(
                f"Graph refused the credentials with {status} for {target}{detail}"
            )
        if status == httpx.codes.NOT_FOUND:
            return not_found(f"Graph answered {status} for {target}{detail}")
        if status == _GONE:
            return gone(f"Graph answered {status} for {target}{detail}")
        # Every other refusal: this one call will never succeed, but the
        # account is fine and the import goes on without that message.
        return MailPermanentError(f"Graph answered {status} for {target}{detail}")

    def _payload(self, target: str, response: httpx.Response) -> dict[str, Any]:
        """The body as a JSON object, or a transient error.

        A 200 that is not JSON is a proxy or a bad gateway day, never Graph:
        the same call a minute later is the thing most likely to work.
        """
        try:
            body = response.json()
        except ValueError as error:
            raise MailTransientError(
                f"Graph answered {target} with something that is not JSON: {error}"
            ) from error
        if not isinstance(body, dict):
            raise MailTransientError(
                f"Graph answered {target} with {type(body).__name__}"
            )
        return body

    def _url(self, target: str) -> str:
        """A path joined onto the configured root, or a whole URL let through.

        Graph is the only provider here whose cursor is a URL, so this is the
        only client that has to accept one — and the only one that has to
        police it. A ``nextLink`` arrives from Graph, but it reaches this method
        by way of an encrypted column and a cursor the engine hands back
        untouched, and every request leaves with a bearer token on it. So a URL
        that does not share :attr:`M365Config.api_base_url`'s origin is never
        sent: it would be this application handing a mailbox's access token to
        whoever wrote the cursor.

        :func:`~mailarc_m365.source.mapping.read_cursor_url` refuses the same
        thing one layer up and more usefully, as an expired cursor the engine
        recovers from. This is the backstop, for a caller that did not go
        through it.
        """
        if not target.lower().startswith(("http://", "https://")):
            return f"{self._config.api_base_url.rstrip('/')}/{target.lstrip('/')}"
        if _origin(target) != _origin(self._config.api_base_url):
            raise MailPermanentError(
                "refusing to send a Microsoft 365 access token to "
                f"{_origin(target)}, which is not {_origin(self._config.api_base_url)}"
            )
        return target


def _origin(url: str) -> str:
    """Scheme and host of a URL, lowercased, port included. Never the path."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{(parts.netloc or '').lower()}"
