"""The consent step — a browser for one mode, and deliberately none for the other.

Two grants, one :data:`~mailarc_core.mail.ports.ConsentRunner`. That asymmetry
is the point of the alias existing at all: the registry asks a provider whether
it has a second step between typing a credential and owning a mailbox, and
Microsoft 365's answer depends on which mode the account chose.

* **Delegated** has one, and it is the only place in this component that opens
  a browser. The user must see Microsoft's own sign-in page, in their own
  browser, signed in as themselves; an application that collected the password
  itself would be the thing OAuth exists to prevent. The only way the code then
  reaches a program on a desktop is a redirect to a loopback address that
  program is listening on, which is
  :mod:`~mailarc_m365.source.loopback`'s whole job.

* **App-only has nothing to consent to here.** A service principal's permission
  was granted once, in the tenant, by an administrator, before the first
  mailbox was ever added — there is no per-account screen and no browser to
  open. So :func:`consent_runner` short-circuits: it validates that the tenant
  and the mailbox are named and that the installation has a client secret, and
  hands back a credential. Registering *no* runner instead would have been
  wrong for the same reason a second descriptor would: the account page reads
  "does this provider have a second step" off the registration, and the answer
  for Microsoft 365 as a whole is yes.

:func:`run_consent` blocks for as long as a human takes, so it may not be
called from a request handler. Hence the same pair
``mailarc_core.graph.status`` uses — a synchronous function and a thin
:func:`run_consent_async` over ``asyncio.to_thread``.
"""

import asyncio
import logging
import webbrowser
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from msal.exceptions import MsalError

from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.ports import ConsentRunner
from mailarc_m365.source.config import M365Config
from mailarc_m365.source.credentials import (
    M365AppOnlyCredentials,
    M365DelegatedCredentials,
    M365FormValues,
)
from mailarc_m365.source.loopback import (
    LoopbackServer,
    RedirectDenied,
    RedirectTimeout,
)
from mailarc_m365.source.model import (
    DELEGATED_SCOPES,
    GRAPH_MAIL_READ_SCOPE,
    GraphTokenResult,
    M365Mode,
)

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "localhost"
"""What the redirect URI names.

``localhost`` and not ``127.0.0.1``: Entra treats the two as different redirect
URIs, and ``http://localhost`` is the one a public-client registration is told
to use — it then accepts any port, which is what lets
:attr:`~mailarc_m365.source.config.M365Config.loopback_port` stay ``0``.
"""

CONSENT_PROMPT = "select_account"
"""Always ask which account, even when the browser has an obvious answer.

A person with a work account and a personal one signed in to the same browser
is the ordinary case, and a silent sign-in as the wrong one produces a mailbox
that verifies, syncs, and archives somebody else's mail under this account's
name. The chooser costs one click; ``login_hint`` preselects the right row.
"""


def run_consent(
    config: M365Config, *, tenant_id: str | None = None, login_hint: str | None = None
) -> M365DelegatedCredentials:
    """Walk the user through Microsoft's sign-in. Blocks until they finish.

    The refresh token this returns is what makes an archive unattended, and it
    is earned by MSAL's own ``offline_access`` — which is why
    :data:`~mailarc_m365.source.model.DELEGATED_SCOPES` must *not* name it:
    MSAL adds the three reserved scopes itself and raises on a caller that
    passes any of them.

    ``login_hint`` is the mailbox the account row names, and
    :data:`CONSENT_PROMPT` still shows the chooser — the hint preselects, it
    does not decide. The caller checks afterwards which mailbox actually
    answered.
    """
    if not config.configured():
        raise MailAuthError(
            "Microsoft 365 is not set up on this installation — "
            "app_m365_client_id names the Entra application to sign in with"
        )
    tenant = (tenant_id or config.default_tenant).strip() or config.default_tenant
    authority = config.authority_for(tenant)
    application = _public_application(config, authority)
    logger.info(
        "Opening a browser for Microsoft 365 consent as client %s at %s",
        config.client_id,
        authority,
    )
    result = _walk_the_browser(application, config, login_hint)
    return _credentials_from(result, tenant_id=tenant)


async def run_consent_async(
    config: M365Config, *, tenant_id: str | None = None, login_hint: str | None = None
) -> M365DelegatedCredentials:
    """:func:`run_consent` off the event loop, so the UI keeps rendering."""
    return await asyncio.to_thread(
        run_consent, config, tenant_id=tenant_id, login_hint=login_hint
    )


def app_only_credentials(
    config: M365Config, values: M365FormValues
) -> M365AppOnlyCredentials:
    """The credential a tenant-wide grant needs, with no round trip at all.

    Everything an app-only mailbox is made of is already known: the tenant that
    issues the token, the mailbox to read, and the client secret that belongs
    to the installation. All three are checked here, because each of them fails
    at Entra with an ``AADSTS`` code that says nothing a user could act on, and
    the point of a consent step is to fail on the page where the fields are.
    """
    tenant = values.tenant_or(config.default_tenant)
    mailbox = values.mailbox_or_address()
    if not config.app_client_secret():
        raise MailAuthError(
            "app-only needs this installation's Entra client secret — "
            "set app_m365_client_secret, or leave the sign-in mode empty "
            "to connect as yourself"
        )
    if not mailbox:
        raise MailAuthError(
            "app-only reads one named mailbox — fill in the Mailbox field, "
            "because a token nobody signed in to has no /me to resolve"
        )
    try:
        return M365AppOnlyCredentials(
            mode=M365Mode.APP_ONLY, tenant_id=tenant, mailbox=mailbox
        )
    except ValueError as error:
        raise MailAuthError(
            f"app-only cannot use the {tenant!r} authority — "
            "fill in the tenant's own id or domain"
        ) from error


def consent_runner(config: M365Config) -> ConsentRunner:
    """The one runner ``app/composition.py`` registers for this provider.

    Bound to a configuration the same way
    :meth:`~mailarc_m365.source.source.M365Source.using` is, and for the same
    reason: the composition root is the only module allowed to build a
    component from configuration, and the
    :data:`~mailarc_core.mail.ports.ConsentRunner` signature has no room for
    one.

    Which of the two paths it takes is read off the credential fields the user
    filled in, never off anything the composition root knows. That keeps the
    two modes inside this component, where the credential model that
    distinguishes them lives.
    """

    async def run(values: Mapping[str, str]) -> str:
        form = M365FormValues.read(values)
        if form.mode is M365Mode.APP_ONLY:
            logger.info("Microsoft 365 app-only account needs no browser consent")
            return app_only_credentials(config, form).to_secret()
        granted = await run_consent_async(
            config,
            tenant_id=form.tenant_or(config.default_tenant),
            login_hint=form.email_address or None,
        )
        return granted.to_secret()

    return run


def _walk_the_browser(
    application: Any, config: M365Config, login_hint: str | None
) -> dict[str, Any]:
    """Start the flow, open the browser, catch the redirect, redeem the code.

    One function because the four steps share one failure mode — nobody
    granted access — and splitting them would mean four call sites each
    translating the same library exception. Everything that goes wrong in here
    leaves as a :class:`~mailarc_core.mail.errors.MailAuthError`; MSAL, the
    browser launcher and the loopback server each fail differently and none of
    their exceptions may escape the adapter.

    The listener is opened *before* the flow is initiated, because the redirect
    URI has to name the port the operating system actually handed out.
    """
    try:
        with LoopbackServer(LOOPBACK_HOST, config.loopback_port) as server:
            flow = application.initiate_auth_code_flow(
                list(DELEGATED_SCOPES),
                redirect_uri=server.redirect_uri,
                prompt=CONSENT_PROMPT,
                login_hint=login_hint,
            )
            _open_browser(str(flow["auth_uri"]))
            response = server.wait(config.consent_timeout)
        result = application.acquire_token_by_auth_code_flow(flow, response)
    except MailAuthError:
        raise
    except RedirectTimeout as error:
        raise MailAuthError(
            f"Microsoft 365 consent did not complete: {error}"
        ) from error
    except RedirectDenied as error:
        raise MailAuthError(f"Microsoft 365 consent was denied ({error})") from error
    except (ValueError, MsalError) as error:
        # ValueError is MSAL's own answer to a state mismatch, which is what a
        # cross-site request forgery attempt looks like from here.
        raise MailAuthError(
            f"Microsoft 365 consent could not be redeemed: {error}"
        ) from error
    except Exception as error:
        raise MailAuthError(
            f"Microsoft 365 consent did not complete: {error}"
        ) from error
    if not isinstance(result, dict) or "access_token" not in result:
        raise MailAuthError(f"Microsoft 365 consent returned no token{_why(result)}")
    return result


def _why(result: Any) -> str:
    """Microsoft's own words about a refusal, as a suffix, or nothing."""
    if not isinstance(result, dict):
        return ""
    described = ": ".join(
        str(result[key]) for key in ("error", "error_description") if result.get(key)
    )
    return f" — {described}" if described else ""


def _credentials_from(
    result: Mapping[str, Any], *, tenant_id: str
) -> M365DelegatedCredentials:
    """MSAL's token dict, reduced to what gets stored.

    A grant without a refresh token is useless to an archive: it would import
    once and then need a human every hour. Better to say so now than to store
    it and fail the first unattended run.
    """
    issued = _validated(result)
    if not issued.refresh_token:
        raise MailAuthError(
            "Microsoft granted access but issued no refresh token — an archive "
            "cannot run unattended without one; connect this mailbox again"
        )
    _require_mailbox_permission(issued)
    return M365DelegatedCredentials(
        tenant_id=tenant_id,
        refresh_token=issued.refresh_token,
        access_token=issued.access_token,
        expires_at=(
            datetime.now(UTC) + timedelta(seconds=issued.expires_in)
            if issued.expires_in
            else None
        ),
        scopes=DELEGATED_SCOPES,
    )


def _validated(result: Mapping[str, Any]) -> GraphTokenResult:
    """The token dict as a model, without the dict reaching an error message."""
    try:
        return GraphTokenResult.model_validate(dict(result))
    except ValueError as error:
        # The input is a token response; pydantic would quote it.
        raise MailAuthError("Microsoft answered with no usable token") from error


def _require_mailbox_permission(issued: GraphTokenResult) -> None:
    """A grant that does not include ``Mail.Read`` is no grant.

    Entra reports the granted scopes by their short name and in no fixed order
    — ``"Mail.Read User.Read profile openid email"`` — so the comparison is on
    the last segment of each, lowercased, rather than on the full URI this
    adapter asked with. A token missing the scope entirely is possible: an
    administrator can restrict which permissions a user is allowed to consent
    to, and the sign-in then succeeds with less than was asked for.
    """
    granted = {
        part.rsplit("/", 1)[-1].lower()
        for part in (issued.scope or "").replace(",", " ").split()
    }
    if not granted:
        # Entra is not required to echo the scopes; an absent list is not a
        # narrowed one, and the first Graph call says so far more reliably.
        return
    wanted = GRAPH_MAIL_READ_SCOPE.rsplit("/", 1)[-1].lower()
    if wanted not in granted:
        raise MailAuthError(
            "Microsoft granted access without the mailbox permission — "
            "this sign-in cannot read mail; connect again and accept Mail.Read"
        )


def _public_application(config: M365Config, authority: str) -> Any:
    """MSAL's public client for the delegated flow.

    A module-level function rather than an inline construction so a test can
    replace it: constructing one reaches ``login.microsoftonline.com`` for the
    authority's metadata, and no test in this component is allowed to.
    """
    import msal

    return msal.PublicClientApplication(
        config.client_id, authority=authority, timeout=config.token_timeout
    )


def _open_browser(url: str) -> None:
    """The default browser, and the one call a test replaces."""
    webbrowser.open(url, new=1, autoraise=True)
