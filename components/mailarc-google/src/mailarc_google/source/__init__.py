"""Gmail behind :class:`~mailarc_core.mail.ports.MailSourcePort`.

The package is named after the capability, not the vendor — ``mailarc_imap.source``
and ``mailarc_m365.source`` will look identical — and it is the whole of what
this component does. Google's field names, its OAuth dance and its JSON stop
here; what leaves is the vocabulary of :mod:`mailarc_core.mail.model`.

One module per concern, layered so nothing points back up:

``model``
    Google's own shapes — endpoints, scopes, the token endpoint's JSON — plus
    ``GMAIL_DESCRIPTOR``, the one declaration that faces the domain.
``config``
    ``GmailConfig`` — where Gmail is and how hard to lean on it. No account.
``credentials``
    ``GmailCredentials`` — what fills ``mail_credentials.secret``, and the
    blocking token refresh in both its shapes.
``loopback``
    The throwaway HTTP server that catches Google's redirect — tolerant of
    browser preconnects, bounded by a deadline, and gone afterwards.
``oauth``
    The installed-app consent on top of it. The one function in this project
    that opens a browser.
``client``
    The HTTP client against the API — the only reader of a status code, and
    the place the error taxonomy is decided.
``mapping``
    Google's JSON turned into domain value objects, and no dictionary out.
``source``
    ``GmailSource`` — the five methods of the port, made of the four above.
"""

from mailarc_google.source.client import GmailApiError, GmailClient
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import (
    ACCESS_TOKEN_LEEWAY,
    REFRESH_TIMEOUT,
    GmailCredentials,
    refresh,
    refresh_async,
)
from mailarc_google.source.model import (
    GMAIL_API_BASE_URL,
    GMAIL_DESCRIPTOR,
    GMAIL_LABELS_SCOPE,
    GMAIL_METADATA_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
    GOOGLE_AUTH_URI,
    GOOGLE_TOKEN_URI,
    GoogleTokenError,
    GoogleTokenResponse,
)
from mailarc_google.source.oauth import run_consent, run_consent_async
from mailarc_google.source.source import GMAIL_MAX_PAGE_SIZE, GmailSource

__all__ = [
    "ACCESS_TOKEN_LEEWAY",
    "GMAIL_API_BASE_URL",
    "GMAIL_DESCRIPTOR",
    "GMAIL_LABELS_SCOPE",
    "GMAIL_MAX_PAGE_SIZE",
    "GMAIL_METADATA_SCOPE",
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SCOPES",
    "GOOGLE_AUTH_URI",
    "GOOGLE_TOKEN_URI",
    "REFRESH_TIMEOUT",
    "GmailApiError",
    "GmailClient",
    "GmailConfig",
    "GmailCredentials",
    "GmailSource",
    "GoogleTokenError",
    "GoogleTokenResponse",
    "refresh",
    "refresh_async",
    "run_consent",
    "run_consent_async",
]
