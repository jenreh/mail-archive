"""What fills ``mail_credentials.secret`` for Gmail, and how it stays fresh.

§8.1 keeps that column structureless on purpose: every provider serialises its
own model into it, so a new one costs no migration. :class:`GmailCredentials`
is Gmail's, which makes the round trip through JSON this module's job rather
than the persistence layer's — :meth:`GmailCredentials.to_secret` and
:meth:`GmailCredentials.from_secret` are the only two ways in and out.

The access token is carried alongside the refresh token so a short run does not
mint a new one before every call. The refresh itself is a form POST to the
token endpoint, done with ``httpx`` and not through ``google-auth``: the status
code is what decides between "ask the user again" and "try again later", and
making the call here is what lets §7.6's taxonomy be read straight off it.

It blocks, so it comes in two shapes — :func:`refresh` and
:func:`refresh_async` — the pair ``mailarc_core.graph.status`` already uses.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from mailarc_core.mail.errors import MailAuthError, MailError, MailTransientError
from mailarc_google.source.model import (
    GMAIL_SCOPES,
    GOOGLE_TOKEN_URI,
    GoogleTokenError,
    GoogleTokenResponse,
    retry_after_seconds,
)

logger = logging.getLogger(__name__)


ACCESS_TOKEN_LEEWAY = 60.0
"""Seconds before expiry at which a token already counts as stale.

A token that expires while a batch is in flight fails the batch. A minute is
longer than any single request is allowed to take, so refreshing that early
means the token outlives whatever is using it.
"""

REFRESH_TIMEOUT = 30.0
"""Seconds a refresh may take. Its own value: this is not an API call."""

_SERVER_ERROR = 500
"""From here up the endpoint is broken, not the credential."""


class GmailCredentials(BaseModel):
    """Everything needed to speak for one Gmail account, minus the mailbox.

    Only what is true of *this mailbox*. The OAuth client the refresh is made
    with belongs to the installation and lives in
    :class:`~mailarc_google.source.config.GmailConfig`; keeping a copy of it
    per account would mean every account still using an old client secret the
    day somebody rotates it.

    ``refresh_token`` is what consent earned and the one field that has to
    survive a restart. ``access_token`` and ``expires_at`` are cache — losing
    them costs one round trip, which is why they are optional and why nothing
    breaks if an older row does not have them.

    Frozen, so a refresh produces a *new* object. The caller therefore cannot
    forget that a fresh refresh token has to go back into ``mail_credentials``:
    it is holding the only copy.
    """

    model_config = ConfigDict(frozen=True)

    refresh_token: str
    token_uri: str = GOOGLE_TOKEN_URI
    scopes: tuple[str, ...] = GMAIL_SCOPES
    access_token: str | None = None
    expires_at: datetime | None = None

    @field_validator("expires_at", mode="after")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        """A naive expiry is UTC.

        google-auth hands back naive UTC and a row written before this model
        existed may hold one. Comparing it to an aware ``now`` raises, so it is
        made aware here rather than at every comparison.
        """
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def needs_refresh(self, *, leeway: float = ACCESS_TOKEN_LEEWAY) -> bool:
        """Whether the cached access token is missing, stale or about to be."""
        if not self.access_token or self.expires_at is None:
            return True
        return self.expires_at <= datetime.now(UTC) + timedelta(seconds=leeway)

    def authorization_header(self) -> str:
        """The ``Authorization`` value for an API call.

        Raises rather than returning an empty header: a request sent without
        one comes back as a 401, and a 401 that was really "we never refreshed"
        would send the user to a re-consent they do not need.
        """
        if not self.access_token:
            raise MailAuthError("no access token — refresh before calling the API")
        return f"Bearer {self.access_token}"

    def with_token(self, token: GoogleTokenResponse) -> GmailCredentials:
        """This account's credentials, carrying what the endpoint just issued.

        Keeps the existing refresh token when the response omits one, which is
        the usual case: Google reissues it only occasionally, and overwriting it
        with nothing would lock the account out.
        """
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=token.expires_in)
            if token.expires_in
            else None
        )
        return self.model_copy(
            update={
                "access_token": token.access_token,
                "expires_at": expires_at,
                "refresh_token": token.refresh_token or self.refresh_token,
            }
        )

    def to_secret(self) -> str:
        """Serialise into ``mail_credentials.secret``, which encrypts it."""
        return self.model_dump_json()

    @classmethod
    def from_secret(cls, secret: str) -> GmailCredentials:
        """Read back what :meth:`to_secret` wrote.

        A row that does not parse is a credential this process cannot use, so
        it fails as one rather than as a ``ValidationError`` nobody upstream
        knows what to do with.

        **The validation error never reaches the message.** pydantic appends
        ``input_value=`` to every complaint, and the input here is the secret
        itself — so interpolating it would copy a ``client_secret`` out of the
        encrypted column and into ``mail_accounts.last_error``, into
        ``mail_sync_jobs.error``, onto the page and into the log, none of which
        are encrypted. ``from error`` keeps the detail on the traceback for
        whoever is holding a debugger; the sentence a human reads carries no
        part of the credential.
        """
        try:
            return cls.model_validate_json(secret)
        except ValueError as error:
            raise MailAuthError(
                "the stored Gmail credentials are unreadable or incomplete — "
                "connect this mailbox again"
            ) from error


def refresh(
    credentials: GmailCredentials,
    *,
    client_id: str,
    client_secret: str,
    timeout: float = REFRESH_TIMEOUT,
) -> GmailCredentials:
    """Trade the refresh token for a new access token. Blocks.

    The client is passed in rather than read off ``credentials`` because it is
    not the account's: one registered OAuth client speaks for the whole
    installation, and it is configuration (§7).

    A single POST per call rather than a pooled client: a refresh happens once
    an hour per account, and a connection held open for that is a connection
    held open for nothing.
    """
    try:
        response = httpx.post(
            credentials.token_uri,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": credentials.refresh_token,
            },
            timeout=timeout,
        )
    except httpx.RequestError as error:
        raise MailTransientError(
            f"token endpoint unreachable at {credentials.token_uri}: {error}"
        ) from error

    if response.status_code != httpx.codes.OK:
        raise _refusal(response)

    try:
        issued = GoogleTokenResponse.model_validate(response.json())
    except ValueError as error:
        # Same reason as `from_secret`: pydantic would quote the input, and the
        # input is the token endpoint's reply — which carries the access token
        # and sometimes a new refresh token.
        raise MailAuthError(
            "the token endpoint answered with no usable token"
        ) from error

    logger.debug(
        "Refreshed a Gmail access token for client %s (rotated=%s)",
        client_id,
        issued.refresh_token is not None,
    )
    return credentials.with_token(issued)


async def refresh_async(
    credentials: GmailCredentials,
    *,
    client_id: str,
    client_secret: str,
    timeout: float = REFRESH_TIMEOUT,
) -> GmailCredentials:
    """:func:`refresh` off the event loop; the call blocks (§10, phase 3)."""
    return await asyncio.to_thread(
        refresh,
        credentials,
        client_id=client_id,
        client_secret=client_secret,
        timeout=timeout,
    )


def _refusal(response: httpx.Response) -> MailError:
    """Turn a token endpoint's non-200 into the error that says what to do.

    The status code decides, not the ``error`` string: a rate limit and a 5xx
    are the same instruction — wait — while everything else the endpoint says
    no to is a credential the user has to grant again.
    """
    status = response.status_code
    detail = _detail(response)
    if status == httpx.codes.TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
        return MailTransientError(
            f"token endpoint returned {status}{detail}",
            retry_after=retry_after_seconds(response.headers.get("Retry-After")),
        )
    return MailAuthError(f"Gmail refused the refresh token ({status}){detail}")


def _detail(response: httpx.Response) -> str:
    """Google's own words about the refusal, as a suffix, or nothing."""
    try:
        described = GoogleTokenError.model_validate(response.json()).describe()
    except ValueError:
        described = ""
    return f" — {described}" if described else ""
