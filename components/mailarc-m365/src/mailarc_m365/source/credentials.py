"""What fills ``mail_credentials.secret`` for Microsoft 365, in two shapes.

That column is structureless on purpose: every provider serialises its own
model into it, so a new one costs no migration. This provider serialises one of
*two*, and that is the whole answer to the question the plan left open —
delegated per user, or app-only per tenant. Both fit in an opaque blob, so both
exist, and a literal ``mode`` field discriminates them. A stored blob therefore
says which kind it is, and :func:`from_secret` cannot silently read a service
principal's credential as a person's.

What separates the two is not decoration:

* A **delegated** credential owns a refresh token a human earned at a consent
  screen, and reads ``/me`` — the mailbox is whoever signed in.
* An **app-only** credential owns nothing durable at all. A service principal
  re-authenticates with the installation's client secret every time, so there
  is no refresh token to rotate and no token to lose; what it does need is the
  mailbox's address, because Graph has no ``/me`` for a request nobody signed
  in to.

The refresh itself goes through MSAL, which is synchronous — it speaks HTTP
through ``requests`` — so :func:`refresh` blocks and comes in the pair
``mailarc_core.graph.status`` already uses, with :func:`refresh_async` putting
it on a thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote

import msal
from msal.exceptions import MsalError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from mailarc_core.mail.errors import MailAuthError, MailError, MailTransientError
from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY
from mailarc_m365.source.model import (
    APP_ONLY_SCOPES,
    COMMON_TENANT,
    DELEGATED_SCOPES,
    GraphTokenError,
    GraphTokenResult,
    M365Mode,
)

logger = logging.getLogger(__name__)

ACCESS_TOKEN_LEEWAY = 60.0
"""Seconds before expiry at which a token already counts as stale.

A token that expires while a batch is in flight fails the batch. A minute is
longer than any single request is allowed to take, so refreshing that early
means the token outlives whatever is using it.
"""

TOKEN_TIMEOUT = 30.0
"""Seconds MSAL may spend at the token endpoint. Its own value, not the API's."""

UNREADABLE = (
    "the stored Microsoft 365 credentials are unreadable or incomplete — "
    "connect this mailbox again"
)
"""The one sentence a broken blob produces, and it quotes nothing.

pydantic appends ``input_value=`` to every complaint, and the input here is the
secret itself — so interpolating a validation error would copy a refresh token
out of the encrypted column into ``mail_accounts.last_error``, into
``mail_sync_jobs.error``, onto the page and into the log, none of which are
encrypted. ``raise ... from error`` keeps the detail on the traceback for
whoever is holding a debugger.
"""

_MODE_WORDS: Mapping[str, M365Mode] = {
    "": M365Mode.DELEGATED,
    "delegated": M365Mode.DELEGATED,
    "user": M365Mode.DELEGATED,
    "app-only": M365Mode.APP_ONLY,
    "app_only": M365Mode.APP_ONLY,
    "apponly": M365Mode.APP_ONLY,
    "application": M365Mode.APP_ONLY,
}
"""Every spelling of the two modes a human may type into the account form.

The form renders a text box, because a
:class:`~mailarc_core.mail.model.CredentialField` declares a name and a label
and nothing else. So the values it can produce are what people write, and
Microsoft's own documentation calls the second one "app-only", "application
permissions" and "app_only" in three different places. An empty box is the
common case and means delegated.
"""


class _M365Credentials(BaseModel):
    """What both shapes have: a tenant, a cached access token, and its expiry.

    Frozen, so a refresh produces a *new* object. The caller therefore cannot
    forget that a rotated token has to go back into ``mail_credentials``: it is
    holding the only copy. ``app/worker.py`` reads it off
    ``source.credentials.to_secret()`` at the end of every run.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str = COMMON_TENANT
    access_token: str | None = None
    expires_at: datetime | None = None

    @field_validator("expires_at", mode="after")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        """A naive expiry is UTC.

        Comparing a naive datetime to an aware ``now`` raises, so it is made
        aware here rather than at every comparison.
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
        """The ``Authorization`` value for a Graph call.

        Raises rather than returning an empty header: a request sent without
        one comes back as a 401, and a 401 that was really "we never refreshed"
        would send the user to a re-consent they do not need.
        """
        if not self.access_token:
            raise MailAuthError("no access token — refresh before calling Graph")
        return f"Bearer {self.access_token}"

    def to_secret(self) -> str:
        """Serialise into ``mail_credentials.secret``, which encrypts it."""
        return self.model_dump_json()

    def _expiry(self, token: GraphTokenResult) -> datetime | None:
        """When the token just issued goes stale, or ``None`` if it never says."""
        if not token.expires_in:
            return None
        return datetime.now(UTC) + timedelta(seconds=token.expires_in)


class M365DelegatedCredentials(_M365Credentials):
    """One person's grant on their own mailbox. No administrator involved.

    The mode a desktop archive wants and the only one a consumer mailbox has.
    ``refresh_token`` is what consent earned and the one field that has to
    survive a restart; ``access_token`` and ``expires_at`` are cache, and
    losing them costs one round trip.

    ``tenant_id`` defaults to ``common``, which accepts work, school and
    personal accounts alike.
    """

    mode: Literal[M365Mode.DELEGATED] = M365Mode.DELEGATED
    refresh_token: str
    scopes: tuple[str, ...] = DELEGATED_SCOPES

    @property
    def mailbox_path(self) -> str:
        """The Graph path prefix for this mailbox.

        ``/me``, because the token *is* a person: Graph resolves it against
        whoever signed in, and there is no id to interpolate.
        """
        return "/me"

    def with_token(self, token: GraphTokenResult) -> Self:
        """This credential, carrying what the token endpoint just issued.

        **Keeps the existing refresh token when the response omits one**, which
        is the usual case: Entra reissues one only occasionally, and
        overwriting it with nothing would lock the account out on the next
        unattended run.
        """
        return self.model_copy(
            update={
                "access_token": token.access_token,
                "expires_at": self._expiry(token),
                "refresh_token": token.refresh_token or self.refresh_token,
            }
        )


class M365AppOnlyCredentials(_M365Credentials):
    """A service principal reading one mailbox in one tenant.

    Nothing here is durable and that is the point: a client-credentials grant
    re-authenticates with the installation's client secret every time, so there
    is no refresh token, nothing to rotate, and nothing a revoked user session
    can take away. What it does carry is the two things Graph cannot infer —
    which tenant issues the token, and which mailbox to read, because a request
    nobody signed in to has no ``/me``.

    ``tenant_id`` may not be ``common``: a client-credentials token is issued
    *by* a tenant, and the multi-tenant authority has none to issue it. Entra
    answers that with ``AADSTS900023``; this says it before the round trip.
    """

    mode: Literal[M365Mode.APP_ONLY]
    tenant_id: str = Field(min_length=1)
    mailbox: str = Field(min_length=1)
    scopes: tuple[str, ...] = APP_ONLY_SCOPES

    @field_validator("tenant_id", mode="after")
    @classmethod
    def _reject_common(cls, value: str) -> str:
        if value.strip().lower() in {COMMON_TENANT, "organizations", "consumers"}:
            raise ValueError(
                "app-only needs one tenant's own id or domain, "
                f"never the shared {value!r} authority"
            )
        return value

    @property
    def mailbox_path(self) -> str:
        """The Graph path prefix for this mailbox.

        ``/users/{address}``. Quoted with ``safe=''`` because an address is
        user input on the path: a ``#`` in a user principal name — legal in
        Entra, and how some on-premises identities are synchronised — would
        otherwise truncate the URL at the fragment.
        """
        return f"/users/{quote(self.mailbox, safe='')}"

    def with_token(self, token: GraphTokenResult) -> Self:
        """This credential, carrying the access token just issued.

        A client-credentials response never carries a refresh token, so unlike
        the delegated shape there is nothing here that could be blanked.
        """
        return self.model_copy(
            update={
                "access_token": token.access_token,
                "expires_at": self._expiry(token),
            }
        )


type M365Credentials = Annotated[
    M365DelegatedCredentials | M365AppOnlyCredentials,
    Field(discriminator="mode"),
]
"""Either shape, told apart by the literal that is stored with them.

A discriminated union rather than one model with optional fields, so that
"delegated with a mailbox set" and "app-only with a refresh token" are not
states this type can be in. pydantic reads ``mode`` first and validates only
the member it names, which is also why a malformed blob produces one clear
complaint instead of two irrelevant ones.
"""

_CREDENTIALS = TypeAdapter[M365Credentials](M365Credentials)
"""Built once: a ``TypeAdapter`` compiles a validator, and this is a hot path."""


def from_secret(secret: str) -> M365Credentials:
    """Read back what :meth:`_M365Credentials.to_secret` wrote.

    A row that does not parse is a credential this process cannot use, so it
    fails as one rather than as a ``ValidationError`` nobody upstream knows
    what to do with — and the sentence it fails with carries no part of the
    secret (:data:`UNREADABLE`).

    It also accepts the *other* shape this column can hold: what the account
    form wrote before anything ran a consent. That blob spells every value as a
    string, including an empty one for every box the user left alone, so the
    defaults have to survive ``""`` — see :func:`_normalised`. An app-only
    mailbox is complete at that point and opens straight from it; a delegated
    one is not, and fails here until consent has put a refresh token in.
    """
    try:
        payload = json.loads(secret)
    except ValueError as error:
        raise MailAuthError(UNREADABLE) from error
    if not isinstance(payload, dict):
        raise MailAuthError(UNREADABLE)
    try:
        return _CREDENTIALS.validate_python(_normalised(payload))
    except ValueError as error:
        raise MailAuthError(UNREADABLE) from error


def _normalised(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The stored mapping with the account form's blank boxes taken out.

    ``mailarc_ui.accounts.state`` writes every declared field, filled or not,
    so ``{"mode": "", "tenant_id": "", "mailbox": ""}`` is what an untouched
    Microsoft 365 form produces. Dropping the empty ones is what lets a
    model default apply — pydantic would otherwise validate ``""`` as a
    perfectly good tenant id and the account would authenticate against an
    authority that is one slash long.
    """
    values = {
        key: value
        for key, value in payload.items()
        if not (isinstance(value, str) and not value.strip())
    }
    values["mode"] = mode_of(payload)
    return values


def mode_of(values: Mapping[str, Any]) -> M365Mode:
    """Which mode a stored blob or a filled-in form names.

    Raises for a word that is neither, rather than defaulting to delegated: a
    person who typed something into that box meant something by it, and
    silently signing in as themselves when they asked for a service principal
    would archive the wrong mailbox under the right name.
    """
    written = str(values.get("mode") or "").strip().lower()
    mode = _MODE_WORDS.get(written)
    if mode is None:
        raise MailAuthError(
            f"{written!r} is not a Microsoft 365 sign-in mode — "
            f"leave it empty for {M365Mode.DELEGATED.value}, "
            f"or write {M365Mode.APP_ONLY.value}"
        )
    return mode


class M365FormValues(BaseModel):
    """What the account form collected, before consent turns it into a credential.

    The input side of a :data:`~mailarc_core.mail.ports.ConsentRunner`: a flat
    mapping of strings keyed by the names
    :data:`~mailarc_m365.source.model.M365_DESCRIPTOR` declared, plus the
    account's own address under
    :data:`~mailarc_core.mail.ports.CONSENT_ADDRESS_KEY`.

    Worth a model rather than three ``values.get`` calls because every one of
    those strings can be empty and each empty one means something different —
    an unset tenant falls back to the configuration, an unset mailbox is fatal
    for app-only and meaningless for delegated.
    """

    model_config = ConfigDict(frozen=True)

    mode: M365Mode = M365Mode.DELEGATED
    tenant_id: str = ""
    mailbox: str = ""
    email_address: str = ""
    """The account row's own address, which the runner uses as a login hint.

    Named for the column it comes from, and the reason a provider's own
    credential field may not be called ``email_address``.
    """

    @classmethod
    def read(cls, values: Mapping[str, str]) -> M365FormValues:
        """One mapping of typed strings, validated and stripped."""
        return cls(
            mode=mode_of(values),
            tenant_id=str(values.get("tenant_id") or "").strip(),
            mailbox=str(values.get("mailbox") or "").strip(),
            email_address=str(values.get(CONSENT_ADDRESS_KEY) or "").strip(),
        )

    def tenant_or(self, fallback: str) -> str:
        """The tenant the user named, or the installation's default."""
        return self.tenant_id or fallback

    def mailbox_or_address(self) -> str:
        """Which mailbox an app-only grant should read.

        The explicit field wins; the account's own address is the sensible
        second answer, because for a shared mailbox the row already names it
        and asking a person to type the same address twice is how the two come
        to disagree.
        """
        return self.mailbox or self.email_address


def refresh(
    credentials: M365Credentials,
    *,
    client_id: str,
    client_secret: str,
    authority: str,
    timeout: float = TOKEN_TIMEOUT,
) -> M365Credentials:
    """Mint a new access token for either shape. Blocks.

    MSAL is synchronous — it speaks HTTP through ``requests`` — so this is the
    blocking half and :func:`refresh_async` is the one an adapter calls.

    The client is passed in rather than read off ``credentials`` because it is
    not the account's: one registered application speaks for the whole
    installation, and it is configuration. A delegated sign-in trades its
    refresh token; an app-only one presents the client secret and gets a token
    for the tenant.

    **Nothing MSAL raises leaves this function.** The library reports a refusal
    as a dict with an ``error`` key and raises only for a fault of its own, so
    both paths are mapped: a ``ValueError`` or an ``MsalError`` is this
    installation's configuration and needs a human, and anything else reaching
    here came out of ``requests`` — a socket, a proxy, a name that did not
    resolve — and is worth trying again.

    The taxonomy is let through untouched, and that ``except`` is load-bearing
    rather than tidy. :func:`_application` raises a
    :class:`~mailarc_core.mail.errors.MailAuthError` for an app-only credential
    on an installation with no client secret — an actionable sentence naming
    the setting — and a ``MailError`` is an ``Exception``, so without this the
    catch-all below would rewrite it as "the token endpoint is unreachable".
    The engine would then back off and retry a configuration that no amount of
    waiting fixes, the account would never reach ``auth_error``, and the UI
    would never offer the one thing that would work.
    """
    try:
        application = _application(
            credentials,
            client_id=client_id,
            client_secret=client_secret,
            authority=authority,
            timeout=timeout,
        )
        result = _acquire(application, credentials)
    except MailError:
        raise
    except (ValueError, MsalError) as error:
        raise MailAuthError(
            f"Microsoft rejected this installation's Entra application: {error}"
        ) from error
    except Exception as error:
        raise MailTransientError(
            f"the Microsoft token endpoint at {authority} is unreachable: {error}"
        ) from error

    if not isinstance(result, dict) or "access_token" not in result:
        raise _refusal(result, authority)

    try:
        issued = GraphTokenResult.model_validate(result)
    except ValueError as error:
        # Same reason as `from_secret`: pydantic quotes its input, and the
        # input here is the token response — access token, and sometimes a new
        # refresh token, in a message that ends up in an unencrypted column.
        raise MailAuthError("Microsoft answered with no usable token") from error

    logger.debug(
        "Refreshed a Microsoft 365 access token for client %s (mode=%s, rotated=%s)",
        client_id,
        credentials.mode.value,
        issued.refresh_token is not None,
    )
    return credentials.with_token(issued)


async def refresh_async(
    credentials: M365Credentials,
    *,
    client_id: str,
    client_secret: str,
    authority: str,
    timeout: float = TOKEN_TIMEOUT,
) -> M365Credentials:
    """:func:`refresh` off the event loop, because every MSAL call blocks."""
    return await asyncio.to_thread(
        refresh,
        credentials,
        client_id=client_id,
        client_secret=client_secret,
        authority=authority,
        timeout=timeout,
    )


def _application(
    credentials: M365Credentials,
    *,
    client_id: str,
    client_secret: str,
    authority: str,
    timeout: float,
) -> Any:
    """The MSAL application for this credential's mode.

    A public client for a delegated sign-in and a confidential one for a
    service principal, which is not a detail: a public client that carried a
    secret would be a secret shipped to every desktop, and MSAL refuses the
    combination outright.

    A module-level function rather than an inline construction so that a test
    can replace it. Everything below it reaches ``login.microsoftonline.com``,
    and no test in this component is allowed to.
    """
    if isinstance(credentials, M365AppOnlyCredentials):
        if not client_secret:
            raise MailAuthError(
                "app-only needs this installation's Entra client secret — "
                "set app_m365_client_secret, or use the delegated mode"
            )
        return msal.ConfidentialClientApplication(
            client_id,
            client_credential=client_secret,
            authority=authority,
            timeout=timeout,
        )
    return msal.PublicClientApplication(client_id, authority=authority, timeout=timeout)


def _acquire(application: Any, credentials: M365Credentials) -> Any:
    """Ask MSAL for a token the way this credential's mode has to ask.

    ``acquire_token_by_refresh_token`` rather than ``acquire_token_silent``:
    silent acquisition reads MSAL's own token cache, and this adapter's cache
    is a row in an encrypted column that outlives the process. Passing the
    stored refresh token explicitly is the documented way in for exactly that
    situation, and its result carries the rotated token when Entra issues one —
    which ``acquire_token_silent`` keeps to itself.
    """
    if isinstance(credentials, M365AppOnlyCredentials):
        return application.acquire_token_for_client(scopes=list(credentials.scopes))
    return application.acquire_token_by_refresh_token(
        credentials.refresh_token, scopes=list(credentials.scopes)
    )


def _refusal(result: Any, authority: str) -> MailError:
    """Turn MSAL's error dict into the error that says what to do about it.

    The one place in this component where a *string* decides rather than a
    status code, and only because MSAL owns the HTTP call and does not surface
    one. RFC 6749 §5.2 names two codes that mean "the endpoint" and every other
    one means "the credential" — see
    :data:`~mailarc_m365.source.model.TRANSIENT_TOKEN_ERRORS`.
    """
    described = GraphTokenError.model_validate(
        result if isinstance(result, dict) else {}
    )
    detail = f": {described.describe()}" if described.describe() else ""
    if described.transient():
        return MailTransientError(f"Microsoft could not issue a token{detail}")
    return MailAuthError(
        f"Microsoft refused the credentials at {authority}{detail} — "
        "connect this mailbox again"
    )
