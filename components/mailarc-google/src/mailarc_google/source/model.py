"""Google's own shapes, and the one declaration that faces the domain.

Everything Google names differently from the rest of the system stops in this
file: the endpoints, the scope string, the token endpoint's JSON and its error
envelope. The vocabulary everyone else shares lives in
:mod:`mailarc_core.mail.model`, and nothing below may leak into it.

:data:`GMAIL_DESCRIPTOR` is the exception that faces both ways. It is a domain
value object, and it is the only place that says what a Gmail account needs
before it can connect — the account form renders those fields and
``app/composition.py`` registers the descriptor. One declaration, so the form
and the registry cannot drift apart.
"""

import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from pydantic import BaseModel, ConfigDict

from mailarc_core.mail.model import (
    MailProvider,
    ProviderDescriptor,
)

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
"""Where the browser is sent for consent. Not configurable: it is Google's.

The ``v2`` endpoint, not the ``/o/oauth2/auth`` one Google's own downloaded
``client_secrets.json`` still names. The legacy path resolves and mostly works,
which is exactly what makes it a bad thing to depend on: every current Google
document for an installed app shows ``v2``, so that is what this sends people
to rather than a URL kept alive for compatibility.
"""

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
"""Default token endpoint. ``GmailConfig`` overrides it so a test can serve it."""

GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1"
"""Default API root, without a trailing slash — paths are joined onto it."""

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
"""The narrowest scope that can read a mailbox.

An archive only ever reads. Asking for anything wider would make the consent
screen ask the user to trust this application with their mail, for a capability
it has no code to use.
"""

GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
"""Read headers and labels without message bodies.

Kept as part of the adapter's public Google vocabulary, but not requested:
``gmail.readonly`` already includes this access and also permits ``format=raw``.
"""

GMAIL_LABELS_SCOPE = "https://www.googleapis.com/auth/gmail.labels"
"""Read and edit labels.

Kept as part of the adapter's public Google vocabulary, but not requested while
the importer has no label-writing behavior.
"""

GMAIL_SCOPES: tuple[str, ...] = (GMAIL_READONLY_SCOPE,)
"""The narrowest permission that can import complete messages.

``gmail.readonly`` already includes message bodies, headers and labels. Asking
for ``gmail.metadata`` as well would add no capability, while ``gmail.labels``
would grant a write permission the importer does not use. The requested scope
must stay in step with the Google Cloud consent screen.
"""


class GoogleTokenResponse(BaseModel):
    """What the token endpoint hands back for a refresh or a code exchange.

    ``refresh_token`` is usually absent: Google reissues one only now and then,
    and a caller that overwrites the stored one with ``None`` locks the account
    out. ``expires_in`` is seconds from *now*, so the absolute expiry can only
    be computed at the call site — which is why it is not a field here.
    """

    model_config = ConfigDict(frozen=True)

    access_token: str
    expires_in: int = 0
    refresh_token: str | None = None
    scope: str | None = None
    token_type: str = "Bearer"  # noqa: S105 - the scheme, not a token


class GoogleTokenError(BaseModel):
    """The token endpoint's failure envelope, as far as it is worth reading.

    Only for the sentence a human sees. The *decision* — re-consent or retry —
    is read off the status code, because ``error`` is a moving target and a
    status code is not.
    """

    model_config = ConfigDict(frozen=True)

    error: str = ""
    error_description: str | None = None

    def describe(self) -> str:
        """The two fields as one line, or an empty string if Google sent none."""
        parts = [part for part in (self.error, self.error_description) if part]
        return ": ".join(parts)


GMAIL_DESCRIPTOR = ProviderDescriptor(
    provider=MailProvider.GMAIL,
    label="Gmail",
    credential_fields=(),
    supports_incremental=False,
)
"""What a Gmail account needs from the user, and what it can do once it has it.

**Nothing.** The OAuth client belongs to the installation and lives in
:class:`~mailarc_google.source.config.GmailConfig`, so a user adding a mailbox
types an address and presses Connect — no client id to paste, no Google Cloud
project to create. The refresh token is not a field either: nobody types one
in, :mod:`~mailarc_google.source.oauth` earns it.

An empty tuple is a real answer here, not a gap. The account form renders
whatever a descriptor declares, so declaring nothing renders nothing, and the
provider that *does* need typing — IMAP, with a host and an app password —
will get its form from the same code.

``supports_incremental`` stays false while listing walks page tokens. Gmail's
``historyId`` makes a delta possible and phase 7 is where it gets built; saying
so before then would promise the engine something the adapter cannot do.
"""


def retry_after_seconds(header: str | None) -> float | None:
    """A ``Retry-After`` header as a number of seconds, in either legal form.

    RFC 9110 allows a delta in seconds or an HTTP-date, and which one arrives
    depends on which of Google's front ends refused — the API and the token
    endpoint do not agree. Both are read, because the alternative is discarding
    the provider's own floor for the engine's backoff (§7.3) half the time.

    Takes the header rather than a response so it stays what it is: a string
    parsed into a number, with no opinion about HTTP. A value in neither form,
    or one already in the past, is simply no floor.
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
        logger.debug("Ignoring unreadable Retry-After header %r", header)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((moment - datetime.now(UTC)).total_seconds(), 0.0)
