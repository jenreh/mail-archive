"""The one place in this project that opens a browser, and why it has to.

Google's installed-app flow has no other shape. The user must see Google's own
consent screen, in their own browser, signed in as themselves — an application
that collected the password itself would be the thing OAuth exists to prevent.
The only way the code then gets back to a program on a desktop is a redirect to
a loopback address that program is listening on. So :func:`run_consent` starts
a :class:`~mailarc_google.source.loopback.LoopbackServer`, opens the browser
at Google's URL, waits for the request that carries the code, and exchanges it.

Nothing else here may do this, and this function may not be called from a
request handler: it blocks for as long as a human takes. Hence the same pair
``mailarc_core.graph.status`` uses — a synchronous function and a thin
:func:`run_consent_async` over ``asyncio.to_thread``.
"""

import asyncio
import logging
import os
import webbrowser
from datetime import UTC, datetime
from typing import Any

from google_auth_oauthlib.flow import InstalledAppFlow

from mailarc_core.mail.errors import MailAuthError
from mailarc_google.source.config import GmailConfig
from mailarc_google.source.credentials import GmailCredentials
from mailarc_google.source.loopback import (
    LoopbackServer,
    RedirectDenied,
    RedirectTimeout,
)
from mailarc_google.source.model import GMAIL_SCOPES, GOOGLE_AUTH_URI

logger = logging.getLogger(__name__)

_LOOPBACK_HOST = "localhost"
"""What the redirect URI names. Google accepts loopback on any port."""

WARNING_PAGE_HINT = (
    "If Google showed 'An error occurred' right after its 'Google hasn't "
    "verified this app' page, switch that page's language once (bottom left) "
    "and continue — the first render of that page is a known Google defect."
)
"""What to tell a user whose consent timed out.

Observed 2026-08-21 across browsers, accounts and projects: for an app in
Google's *Testing* publishing status, Continue on the unverified-app warning
answers HTTP 500 until the page has been re-rendered once. Nothing on this
side can trigger that re-render, so the sentence is the fix.
"""


def run_consent(
    config: GmailConfig, *, login_hint: str | None = None
) -> GmailCredentials:
    """Walk the user through Google's consent screen. Blocks until they finish.

    ``access_type=offline`` and ``prompt=consent`` together are what earn a
    refresh token. Without them Google hands back an access token that dies in
    an hour and an account that can never sync again unattended — and it hands
    back no refresh token at all on a *second* consent, which is exactly when
    nobody is watching.

    ``login_hint`` is the mailbox the account row names. Google then preselects
    it instead of showing the chooser, which is what keeps a person with three
    Google sessions from consenting as the wrong one; the caller still checks
    the identity the mailbox reports afterwards.
    """
    flow = InstalledAppFlow.from_client_config(
        _client_config(config), scopes=list(GMAIL_SCOPES)
    )
    logger.info("Opening a browser for Gmail consent as client %s", config.client_id)
    try:
        with LoopbackServer(_LOOPBACK_HOST, config.loopback_port) as server:
            flow.redirect_uri = server.redirect_uri
            url, _state = flow.authorization_url(**_authorization_params(login_hint))
            _open_browser(url)
            path = server.wait(config.consent_timeout)
            redirect_uri = server.redirect_uri
        # oauthlib only accepts an https response URL; the state check still
        # runs on it. The relaxed scope check lets a grant the user narrowed on
        # Google's per-scope consent page come back as data instead of an
        # exception, so the sentence below can say what is missing.
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow.fetch_token(
            authorization_response=redirect_uri.replace("http://", "https://", 1)
            + path.lstrip("/")
        )
    except MailAuthError:
        raise
    except RedirectTimeout as error:
        raise MailAuthError(
            f"Gmail consent did not complete: {error}. {WARNING_PAGE_HINT}"
        ) from error
    except RedirectDenied as error:
        raise MailAuthError(f"Gmail consent was denied ({error})") from error
    except Exception as error:
        # oauthlib, the browser launcher and the token endpoint each fail
        # differently; from here they are one thing — nobody granted access —
        # and that is a MailAuthError, never a stray library exception
        # escaping the adapter (§7.6).
        raise MailAuthError(f"Gmail consent did not complete: {error}") from error

    granted = flow.credentials
    _require_scopes(granted)
    return _credentials_from(granted, config)


async def run_consent_async(
    config: GmailConfig, *, login_hint: str | None = None
) -> GmailCredentials:
    """:func:`run_consent` off the event loop, so the UI keeps rendering."""
    return await asyncio.to_thread(run_consent, config, login_hint=login_hint)


def _open_browser(url: str) -> None:
    """The default browser, and the one call a test replaces with a socket."""
    webbrowser.open(url, new=1, autoraise=True)


def _authorization_params(login_hint: str | None) -> dict[str, str]:
    params = {"access_type": "offline", "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    return params


def _client_config(config: GmailConfig) -> dict[str, Any]:
    """The client secrets document, built rather than read from a file.

    Google's tooling hands out a JSON file; whoever deploys this puts its two
    fields into the configuration instead, so there is no file to lose, no
    path to get wrong, and no user who has to be walked through the Google
    Cloud console before they can add a mailbox.
    """
    return {
        "installed": {
            "client_id": config.client_id,
            "client_secret": config.oauth_client_secret(),
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": config.token_uri,
            "redirect_uris": [f"http://{_LOOPBACK_HOST}"],
        }
    }


def _granted_scopes(granted: Any) -> tuple[str, ...]:
    """What Google actually granted, falling back to what was requested.

    google-auth fills ``granted_scopes`` from the token response when Google
    includes one — and Google does, because its consent page lets the user
    untick any scope individually.
    """
    scopes = getattr(granted, "granted_scopes", None) or getattr(
        granted, "scopes", None
    )
    return tuple(scopes or ())


def _require_scopes(granted: Any) -> None:
    """A grant the user narrowed on Google's per-scope page is no grant."""
    missing = [scope for scope in GMAIL_SCOPES if scope not in _granted_scopes(granted)]
    if missing:
        raise MailAuthError(
            "Google granted access without the mailbox permission — tick the "
            "Gmail checkbox on the consent page and connect again"
        )


def _credentials_from(granted: Any, config: GmailConfig) -> GmailCredentials:
    """google-auth's credentials object, reduced to what gets stored.

    A grant without a refresh token is useless to an archive: it would import
    once and then need a human every hour. Better to say so now than to store
    it and fail the first unattended run.
    """
    refresh_token = getattr(granted, "refresh_token", None)
    if not refresh_token:
        raise MailAuthError(
            "Google granted access but issued no refresh token — revoke this "
            "application's access in the Google account and consent again"
        )
    return GmailCredentials(
        refresh_token=refresh_token,
        token_uri=getattr(granted, "token_uri", None) or config.token_uri,
        scopes=_granted_scopes(granted) or GMAIL_SCOPES,
        access_token=getattr(granted, "token", None),
        expires_at=_aware(getattr(granted, "expiry", None)),
    )


def _aware(expiry: datetime | None) -> datetime | None:
    """google-auth reports expiry as naive UTC; everything here is aware."""
    if expiry is None or expiry.tzinfo is not None:
        return expiry
    return expiry.replace(tzinfo=UTC)
