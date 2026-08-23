"""Microsoft Graph behind :class:`~mailarc_core.mail.ports.MailSourcePort`.

The package is named after the capability, not the vendor —
``mailarc_google.source`` and ``mailarc_imap.source`` look identical — and it
is the whole of what this component does. Graph's ``@odata`` annotations, its
mail-folder ids and its token dance stop here; what leaves is the vocabulary of
:mod:`mailarc_core.mail.model`.

One module per concern, layered so nothing points back up:

``model``
    Graph's own shapes — endpoints, the two scope sets, the ``@odata`` names,
    MSAL's token dict — plus ``M365_DESCRIPTOR``, the one declaration that
    faces the domain.
``config``
    ``M365Config`` — where Graph is, which Entra application speaks for us, and
    which folder the delta runs over. No account.
``credentials``
    The two shapes ``mail_credentials.secret`` can hold, told apart by a
    literal ``mode``, and the MSAL refresh in both its blocking and its
    threaded form.
``loopback``
    The throwaway HTTP server that catches the redirect — tolerant of browser
    preconnects, bounded by a deadline, and gone afterwards.
``oauth``
    The delegated consent on top of it, and the deliberate absence of one for
    app-only. The only module here that opens a browser.
``client``
    The HTTP client against Graph — the only reader of a status code, the
    place the error taxonomy is decided, and the only thing that will not send
    a bearer token to a URL that is not Graph.
``mapping``
    Graph's JSON turned into domain value objects, and no dictionary out —
    including the cursor, which for this provider is a whole URL.
``source``
    ``M365Source`` — the six methods of the port, made of the rest.
"""

from mailarc_m365.source.client import GraphApiError, GraphClient
from mailarc_m365.source.config import M365Config
from mailarc_m365.source.credentials import (
    ACCESS_TOKEN_LEEWAY,
    TOKEN_TIMEOUT,
    M365AppOnlyCredentials,
    M365Credentials,
    M365DelegatedCredentials,
    M365FormValues,
    from_secret,
    mode_of,
    refresh,
    refresh_async,
)
from mailarc_m365.source.model import (
    APP_ONLY_SCOPES,
    COMMON_TENANT,
    DELEGATED_SCOPES,
    GRAPH_API_BASE_URL,
    GRAPH_DEFAULT_SCOPE,
    GRAPH_MAIL_READ_SCOPE,
    GRAPH_USER_READ_SCOPE,
    M365_DESCRIPTOR,
    MESSAGE_SELECT,
    MICROSOFT_AUTHORITY_HOST,
    RESYNC_REQUIRED,
    GraphTokenError,
    GraphTokenResult,
    M365Mode,
    retry_after_seconds,
)
from mailarc_m365.source.oauth import (
    app_only_credentials,
    consent_runner,
    run_consent,
    run_consent_async,
)
from mailarc_m365.source.source import GRAPH_MAX_PAGE_SIZE, M365Source

__all__ = [
    "ACCESS_TOKEN_LEEWAY",
    "APP_ONLY_SCOPES",
    "COMMON_TENANT",
    "DELEGATED_SCOPES",
    "GRAPH_API_BASE_URL",
    "GRAPH_DEFAULT_SCOPE",
    "GRAPH_MAIL_READ_SCOPE",
    "GRAPH_MAX_PAGE_SIZE",
    "GRAPH_USER_READ_SCOPE",
    "M365_DESCRIPTOR",
    "MESSAGE_SELECT",
    "MICROSOFT_AUTHORITY_HOST",
    "RESYNC_REQUIRED",
    "TOKEN_TIMEOUT",
    "GraphApiError",
    "GraphClient",
    "GraphTokenError",
    "GraphTokenResult",
    "M365AppOnlyCredentials",
    "M365Config",
    "M365Credentials",
    "M365DelegatedCredentials",
    "M365FormValues",
    "M365Mode",
    "M365Source",
    "app_only_credentials",
    "consent_runner",
    "from_secret",
    "mode_of",
    "refresh",
    "refresh_async",
    "retry_after_seconds",
    "run_consent",
    "run_consent_async",
]
