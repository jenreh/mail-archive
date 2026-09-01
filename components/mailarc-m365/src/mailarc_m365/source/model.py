"""Graph's own shapes, and the one declaration that faces the domain.

Everything Microsoft names differently from the rest of the system stops in
this file: the Graph endpoints, the two scope sets, the ``@odata`` annotations
and the token result MSAL hands back. The vocabulary everyone else shares lives
in :mod:`mailarc_core.mail.model`, and nothing below may leak into it.

:data:`M365_DESCRIPTOR` is the exception that faces both ways. It is a domain
value object, and it is the only place that says what a Microsoft 365 account
needs before it can connect — the account form renders those fields and
``app/composition.py`` registers the descriptor.

**Two sign-in modes, one provider.** The plan left the choice between a
delegated (per-user) and an app-only (per-tenant) grant open and observed that
the opaque credential blob carries both. It does, and both are implemented:
:class:`M365Mode` is the literal that discriminates them, so a stored blob says
which it is and no reader can mistake one for the other. Which one a mailbox
uses is a property of *that mailbox*, not of the installation — a tenant may
have one service principal reading a shared mailbox and a person signing in to
their own — so the mode is a credential field and not a setting.
"""

import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from mailarc_core.mail.model import (
    CredentialField,
    MailProvider,
    ProviderDescriptor,
)

logger = logging.getLogger(__name__)

GRAPH_API_BASE_URL = "https://graph.microsoft.com/v1.0"
"""Default API root, without a trailing slash — paths are joined onto it.

``v1.0`` and never ``beta``: an archive is a thing people keep for years, and
the beta endpoint is documented as subject to breaking change without notice.
"""

MICROSOFT_AUTHORITY_HOST = "https://login.microsoftonline.com"
"""Where tokens are minted. A setting only so a test can serve it."""

COMMON_TENANT = "common"
"""The multi-tenant authority: work, school and personal accounts alike.

The right default for a desktop archive, whose user is whoever is sitting in
front of it. An app-only grant cannot use it — a client-credentials token is
issued *by* a tenant and there is no tenant here to issue it — which
:class:`~mailarc_m365.source.credentials.M365AppOnlyCredentials` enforces
rather than letting Entra answer with ``AADSTS900023``.
"""

GRAPH_MAIL_READ_SCOPE = "https://graph.microsoft.com/Mail.Read"
"""The narrowest delegated permission that can read a mailbox.

An archive only ever reads. ``Mail.ReadWrite`` would ask a person to trust this
application with deleting their mail, for a capability it has no code to use.
"""

GRAPH_USER_READ_SCOPE = "https://graph.microsoft.com/User.Read"
"""The narrowest permission that answers ``verify()``'s question.

Not padding. ``Mail.Read`` grants ``/me/messages`` and nothing else, so a token
carrying it alone is refused at ``GET /me`` with a 403 — and
:meth:`~mailarc_core.mail.ports.MailSourcePort.verify` has to report *whose*
mailbox this is, which on a consent screen a person can open as any of three
accounts they are signed in to. ``User.Read`` is the least-privilege way to ask
the identity provider that one question.
"""

DELEGATED_SCOPES: tuple[str, ...] = (GRAPH_MAIL_READ_SCOPE, GRAPH_USER_READ_SCOPE)
"""What the delegated consent asks a human to grant.

``openid``, ``profile`` and ``offline_access`` are deliberately **absent**, and
that is not an oversight: MSAL adds all three itself and raises ``ValueError``
if a caller passes any of them (``ClientApplication._decorate_scope``, msal
1.37.0). The refresh token this adapter stores comes from the
``offline_access`` MSAL adds, not from one named here.
"""

GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
"""The only scope a client-credentials grant may ask for.

App-only permissions are granted by an administrator in the tenant, not by a
scope in the request: ``.default`` means "everything already consented for this
application", which for this one is the ``Mail.Read`` *application* permission.
Asking for ``Mail.Read`` by name in a client-credentials request is an
``AADSTS1002012``.
"""

APP_ONLY_SCOPES: tuple[str, ...] = (GRAPH_DEFAULT_SCOPE,)
"""What a service principal asks for. See :data:`GRAPH_DEFAULT_SCOPE`."""

MESSAGE_SELECT = "id,conversationId,parentFolderId,categories"
"""The message properties a listing needs, and not one more.

Graph's default projection of a message includes ``body``, so a listing without
``$select`` downloads the whole mailbox twice — once as JSON here and once as
MIME in :meth:`~mailarc_m365.source.source.M365Source.fetch_raw`. Each of these
four earns its place: ``id`` addresses the message, ``conversationId`` is
Graph's thread, and ``parentFolderId`` plus ``categories`` are what the archive
hangs on it as labels (see
:func:`~mailarc_m365.source.mapping.message_ref`).

**The same string has to be used for the watermark's drain as for the delta
itself.** Graph bakes the query options into the ``deltaLink`` it hands back,
so a drain that selected less would mint a cursor whose later pages carry no
folder and no categories, and the labels would go missing on exactly the
messages a delta brings in.
"""

DELTA_CHANGE_TYPE = "created"
"""The only kind of change this archive can act on.

Graph's message delta also reports ``updated`` and ``deleted``. An archive
keeps what it was given — a mail deleted in Outlook next week was still
received this week — so the other two produce records it would only discard,
and asking for one type keeps a quiet mailbox down to a single call.
"""

RESYNC_REQUIRED = "resyncRequired"
"""Graph's code for "that token is too old", sent with a 410.

Read for the log line only. The *decision* is the status code plus the question
the caller asked — see :meth:`~mailarc_m365.source.client.GraphClient.get_json`
— because a code is stable in a way an error string is not.
"""

NEXT_LINK = "@odata.nextLink"
DELTA_LINK = "@odata.deltaLink"
COUNT = "@odata.count"
REMOVED = "@removed"
"""The four ``@odata`` annotations this adapter reads. Nothing else crosses out.

``@removed`` marks a message Graph is reporting as gone. It cannot appear while
:data:`DELTA_CHANGE_TYPE` is ``created``, and it is skipped anyway: an archive
that deleted on Microsoft's say-so would not be an archive.
"""


class M365Mode(StrEnum):
    """Which kind of grant opens this mailbox.

    The literal that discriminates the credential blob. Spelled with a hyphen
    for ``app-only`` because that is what Microsoft's own documentation calls
    it, and a person reading the account form should recognise the word.
    """

    DELEGATED = "delegated"
    APP_ONLY = "app-only"


class GraphTokenResult(BaseModel):
    """What MSAL hands back for a code exchange, a refresh or a client grant.

    MSAL returns a plain ``dict``: the token endpoint's JSON with its telemetry
    keys stripped. Validating it into a model here is what keeps the rest of
    the adapter from indexing into an untyped mapping — and what makes the one
    field that matters explicit.

    ``refresh_token`` is usually **absent**. Entra reissues one only now and
    then, and a caller that overwrites the stored one with ``None`` locks the
    account out; :meth:`~mailarc_m365.source.credentials.M365DelegatedCredentials.with_token`
    is where that is prevented. A client-credentials result never carries one
    at all — a service principal re-authenticates with its own secret.

    ``expires_in`` is seconds from *now*, so the absolute expiry can only be
    computed at the call site, which is why it is not a field here.
    """

    model_config = ConfigDict(frozen=True)

    access_token: str
    expires_in: int = 0
    refresh_token: str | None = None
    scope: str | None = None
    token_type: str = "Bearer"  # noqa: S105 - the scheme, not a token


class GraphTokenError(BaseModel):
    """MSAL's failure dict, as far as it is worth reading.

    A result with an ``error`` key is how MSAL reports a refusal; it raises
    only for a fault of its own. ``error`` is also the one string this adapter
    *does* branch on, because the token endpoint answers every refusal with the
    same HTTP status and MSAL does not surface it — see
    :data:`TRANSIENT_TOKEN_ERRORS`.
    """

    model_config = ConfigDict(frozen=True)

    error: str = ""
    error_description: str | None = None

    def describe(self) -> str:
        """The two fields as one line, or an empty string if Entra sent none.

        ``error_description`` carries the ``AADSTS`` code an administrator
        needs, and no part of a token: MSAL puts the credential nowhere near
        it.
        """
        parts = [part for part in (self.error, self.error_description) if part]
        return ": ".join(parts)

    def transient(self) -> bool:
        """Whether waiting could fix this. See :data:`TRANSIENT_TOKEN_ERRORS`."""
        return self.error.strip().lower() in TRANSIENT_TOKEN_ERRORS


TRANSIENT_TOKEN_ERRORS = frozenset({"temporarily_unavailable", "server_error"})
"""OAuth 2.0 error codes that mean "the endpoint, not the credential".

The exception to this adapter's own rule that the status code decides. MSAL
owns the HTTP call and hands back a dict, so there *is* no status code here to
read — these two are RFC 6749 §5.2's own names for a refusal a retry may fix,
and every other code in that section names a credential a human has to grant
again.
"""

M365_DESCRIPTOR = ProviderDescriptor(
    provider=MailProvider.M365,
    label="Microsoft 365",
    credential_fields=(
        CredentialField(
            name="mode",
            label="Sign-in mode",
            required=False,
            placeholder="delegated (leave empty) or app-only",
        ),
        CredentialField(
            name="tenant_id",
            label="Directory (tenant) ID",
            required=False,
            placeholder="common, or the tenant's GUID — required for app-only",
        ),
        CredentialField(
            name="mailbox",
            label="Mailbox (app-only only)",
            required=False,
            placeholder="the address Graph should read, e.g. team@contoso.com",
        ),
    ),
    supports_incremental=True,
)
"""What a Microsoft 365 account needs from the user, and what it can do with it.

Three fields, none of them required and none of them a secret. The OAuth client
belongs to the *installation* and lives in
:class:`~mailarc_m365.source.config.M365Config`, exactly as Gmail's does, so
nobody adding a mailbox is asked to create an Entra app registration.

The account form renders whatever a descriptor declares, with no type beyond
"text" and no way to offer a choice, so ``mode`` is a word a person types and
an empty box means :attr:`M365Mode.DELEGATED` — the mode a desktop archive and
a personal mailbox both want, and the only one that needs no administrator. The
alternative would have been two descriptors, which would put two Microsoft
entries in the provider list and make "which of these am I?" the first question
a user has to answer.

``tenant_id`` defaults to :data:`COMMON_TENANT` for a delegated sign-in and is
required for app-only. ``mailbox`` is meaningless for a delegated grant — the
person who signs in *is* the mailbox — and required for app-only, where nobody
signs in and Graph has no ``/me`` to resolve.

``supports_incremental`` is **true**, and
:meth:`~mailarc_m365.source.source.M365Source.watermark` is what has to keep
that promise: it returns a ``deltaLink``, never ``None``. The scheduler reads
this flag before it queues a delta, so a descriptor that claimed one while the
watermark answered ``None`` would have the account queued forever and fetch
nothing.
"""


def retry_after_seconds(header: str | None) -> float | None:
    """A ``Retry-After`` header as a number of seconds, in either legal form.

    RFC 9110 allows a delta in seconds or an HTTP-date. Graph documents the
    seconds form for its 429s and its front doors are not all the same
    software, so both are read — the alternative is discarding the provider's
    own floor for the engine's backoff half the time.

    Takes the header rather than a response so it stays what it is: a string
    parsed into a number, with no opinion about HTTP. A value in neither form,
    or one already in the past, is simply no floor.

    Duplicated from :mod:`mailarc_google.source.model` on purpose — a component
    may not import a sibling, and the shared home this belongs in would be a
    change to ``mailarc-core``.
    """
    if not header:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(header)
    except TypeError, ValueError:
        logger.debug("Ignoring an unreadable Retry-After header %r", header)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((moment - datetime.now(UTC)).total_seconds(), 0.0)
