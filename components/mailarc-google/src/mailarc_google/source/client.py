"""The HTTP half of the adapter, and the only place that reads a status code.

Three jobs and no fourth: put a live access token on every request, turn what
Gmail answers into the taxonomy of §7.6, and close the connection pool. What
the JSON *means* is :mod:`~mailarc_google.source.mapping`'s business; which
call to make is :mod:`~mailarc_google.source.source`'s.

**No ``httpx`` exception leaves this module.** §7.1 is blunt about why: an
adapter that lets one through has not decided whether the engine should retry,
and the engine has no way to decide for it.

**It does not retry either.** The engine already backs off with jitter and
respects ``Retry-After`` (§7.3); a second loop underneath it would multiply
every wait by a number invisible from the outside. The single exception is a
401, which usually means the access token aged out mid-run: the client
refreshes **once** and repeats the call. A second 401 is a credential the user
has to grant again, not a clock.

**One status has no meaning without the question.** A 404 says "this message is
gone" when a message was asked for and "this history is gone" when history was,
and those are different answers — skip one message, or throw the cursor away and
walk the whole mailbox (§7.6). The status alone cannot tell them apart, so
:meth:`GmailClient.get` takes a ``not_found`` type and the call site names which
it asked for. The decision still happens here, in the one module allowed to read
a status code; only the *meaning* travels in, and only for 404. Catching
:class:`~mailarc_core.mail.errors.MailPermanentError` at the history call site
instead would have re-decided on the type and swept up a 400, a 410 and a
proxy's 451 as "expired cursor" — a pointless re-walk of somebody's mailbox
because a query parameter was misspelled.
"""

import logging
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import GmailCredentials, refresh_async
from mailarc_google.source.model import retry_after_seconds

logger = logging.getLogger(__name__)

_SERVER_ERROR = 500
"""From here up the API is broken, not the request."""

_RATE_LIMIT_REASONS = frozenset(
    {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded"}
)
"""Reasons Gmail sends with a 403 when it means "too fast", not "no".

Gmail spends its 250 units/user/s (§11) as a 403 at least as often as a 429.
Reading one of these as an auth failure would put a perfectly good account into
``auth_error`` and send the user to a re-consent that fixes nothing.
"""


class GmailApiError(BaseModel):
    """Gmail's refusal, reduced to the two parts that are worth reading.

    The *decision* comes off the status code — a code is stable and Google's
    ``message`` is not. ``reasons`` is the one exception, and only to tell a
    quota 403 from a real one.
    """

    model_config = ConfigDict(frozen=True)

    message: str = ""
    reasons: tuple[str, ...] = ()

    @classmethod
    def read(cls, response: httpx.Response) -> GmailApiError:
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
            message=str(envelope.get("message") or ""),
            reasons=_reasons(envelope.get("errors")),
        )

    def describe(self) -> str:
        """Google's own words about the refusal, or an empty string."""
        return self.message

    def rate_limited(self) -> bool:
        """Whether this refusal is a quota, whatever its status code says."""
        return any(reason.lower() in _RATE_LIMIT_REASONS for reason in self.reasons)


class GmailClient:
    """Authenticated GETs against one account's Gmail API.

    It holds the credentials because it is the only thing that can notice they
    went stale. They are frozen, so a refresh makes a *new* object and
    :attr:`credentials` is the only copy of it — phase 3 item 3 wants a rotated
    refresh token back in ``mail_credentials``, and this attribute is where the
    owner reads it from.
    """

    def __init__(
        self, credentials: GmailCredentials, config: GmailConfig | None = None
    ) -> None:
        self._config = config or GmailConfig()
        self._credentials = credentials
        self._http = httpx.AsyncClient(timeout=self._config.request_timeout)
        self._closed = False

    @property
    def credentials(self) -> GmailCredentials:
        """The credentials as they stand now, refreshes included."""
        return self._credentials

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        not_found: type[MailError] = MailPermanentError,
    ) -> dict[str, Any]:
        """One GET below the API root, with the taxonomy already applied.

        Returns the decoded JSON object. Everything else — a refusal, a broken
        connection, a body that is not JSON — leaves as one of the four
        errors, so no caller of this method has to know what ``httpx`` is.

        ``not_found`` is what a **404** means for *this* question, and the
        default is the answer for every question but one: the resource is gone,
        so skip it and keep going. The history walk passes
        :class:`~mailarc_core.mail.errors.MailCursorExpired` instead, because
        Gmail says "your ``startHistoryId`` is too old" with the same code and
        that one is a full resync, not a skipped message. Only 404 is
        redirected; every other refusal keeps the meaning this module gives it.
        """
        response = await self._send(path, params)
        if response.status_code == httpx.codes.UNAUTHORIZED:
            await self._refresh()
            response = await self._send(path, params)
        if response.status_code != httpx.codes.OK:
            raise self._refusal(path, response, not_found)
        return self._payload(path, response)

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call twice (§7.1)."""
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()

    async def _send(
        self, path: str, params: Mapping[str, str | int] | None
    ) -> httpx.Response:
        """One request, with a token that is alive when it goes out.

        The pre-flight refresh is what keeps the 401 path rare: a token that
        expires between two pages of a long import would otherwise cost a
        wasted round trip per call for the rest of the batch.
        """
        if self._credentials.needs_refresh():
            await self._refresh()
        headers = {"Authorization": self._credentials.authorization_header()}
        try:
            return await self._http.get(
                self._url(path), params=dict(params or {}), headers=headers
            )
        except httpx.TimeoutException as error:
            raise MailTransientError(f"Gmail timed out on {path}: {error}") from error
        except httpx.RequestError as error:
            raise MailTransientError(
                f"Gmail is unreachable for {path}: {error}"
            ) from error

    async def _refresh(self) -> None:
        """Mint a new access token and keep whatever came back with it.

        Raises out of :func:`~mailarc_google.source.credentials.refresh_async`
        unchanged: a rejected refresh token is terminal and a 5xx at the token
        endpoint is worth trying again, and both decisions were already made
        there.
        """
        previous = self._credentials.refresh_token
        self._credentials = await refresh_async(
            self._credentials,
            client_id=self._config.client_id,
            client_secret=self._config.oauth_client_secret(),
        )
        if self._credentials.refresh_token != previous:
            logger.info(
                "Gmail rotated the refresh token of an account — it has to be "
                "written back to mail_credentials"
            )

    def _refusal(
        self,
        path: str,
        response: httpx.Response,
        not_found: type[MailError] = MailPermanentError,
    ) -> MailError:
        """Turn a non-200 into the error that says what to do about it."""
        status = response.status_code
        described = GmailApiError.read(response)
        detail = f" — {described.describe()}" if described.describe() else ""
        if (
            status == httpx.codes.TOO_MANY_REQUESTS
            or status >= _SERVER_ERROR
            or (status == httpx.codes.FORBIDDEN and described.rate_limited())
        ):
            return MailTransientError(
                f"Gmail returned {status} for {path}{detail}",
                retry_after=retry_after_seconds(response.headers.get("Retry-After")),
            )
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            return MailAuthError(
                f"Gmail refused the credentials with {status} for {path}{detail}"
            )
        if status == httpx.codes.NOT_FOUND:
            # The only status whose meaning the caller owns. "Gone" is the
            # default and covers a message deleted between listing and fetch;
            # the history walk reads it as a cursor Gmail no longer keeps.
            return not_found(f"Gmail answered {status} for {path}{detail}")
        # Every other refusal: this one call will never succeed, but the
        # account is fine and the import goes on without that message.
        return MailPermanentError(f"Gmail answered {status} for {path}{detail}")

    def _payload(self, path: str, response: httpx.Response) -> dict[str, Any]:
        """The body as a JSON object, or a transient error.

        A 200 that is not JSON is a proxy or a bad gateway day, never Gmail:
        the same call a minute later is the thing most likely to work.
        """
        try:
            body = response.json()
        except ValueError as error:
            raise MailTransientError(
                f"Gmail answered {path} with something that is not JSON: {error}"
            ) from error
        if not isinstance(body, dict):
            raise MailTransientError(
                f"Gmail answered {path} with {type(body).__name__}"
            )
        return body

    def _url(self, path: str) -> str:
        """Join a path onto the configured root, whatever the slashes look like."""
        return f"{self._config.api_base_url.rstrip('/')}/{path.lstrip('/')}"


def _reasons(errors: object) -> tuple[str, ...]:
    """The ``reason`` of every detail in Google's error envelope."""
    if not isinstance(errors, list):
        return ()
    return tuple(
        str(one["reason"])
        for one in errors
        if isinstance(one, dict) and one.get("reason")
    )
