"""Where Graph is, which Entra application speaks for us, and how hard we lean.

Nothing here names a mailbox. Which account, whose refresh token and how far
the last run got are **state in SQLite** — a second Microsoft 365 account must
not need a second config. The one apparent exception,
:attr:`M365Config.delta_folder`, is not one: it names a *folder inside every*
mailbox, the same one for all of them.

The Entra application is the opposite: it belongs to the *installation*, not to
any one mailbox. Every account this deployment connects goes through the same
registered client, so it is configured once by whoever runs the application and
no user is ever asked to create an app registration. That is also why the
client secret lives here rather than on the credential: two copies of a secret
drift the day somebody rotates it, and the one in the database would go on
being used until every account broke.

The two URLs are settings rather than constants for one reason: a test has to
be able to point them at a local HTTP server. No test in this component talks
to Microsoft.
"""

from appkit_commons.configuration.base import BaseConfig
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from mailarc_m365.source.model import (
    COMMON_TENANT,
    GRAPH_API_BASE_URL,
    MICROSOFT_AUTHORITY_HOST,
)


class M365Config(BaseConfig):
    """The Entra application, the endpoints, the timeouts and the page sizes."""

    model_config = SettingsConfigDict(
        env_prefix="app_m365_",
        env_file=".env",
        populate_by_name=True,
    )

    client_id: str = ""
    """The Entra application this installation speaks as.

    Empty by default because there is no sensible one to ship: the registration
    is whoever deploys this, and the consent screen and the Graph quota are
    theirs. Empty simply means Microsoft 365 is not set up, and
    :meth:`configured` is how the application asks.

    One registration serves both modes, but not with the same platform: the
    delegated flow needs a *public client* redirect (``http://localhost`` under
    "Mobile and desktop applications", which permits any loopback port), and
    app-only needs a client secret and the ``Mail.Read`` **application**
    permission with tenant admin consent. A registration set up for only one of
    the two works for only one of the two, and says so with an ``AADSTS``
    code.
    """

    client_secret: SecretStr | None = None
    """The secret belonging to :attr:`client_id`. App-only only.

    A delegated sign-in must not have one — a public client that carries a
    secret is a secret shipped to every desktop — so this is unset on a
    desktop installation and :meth:`configured` does not ask for it.

    A ``SecretStr`` so it cannot fall into a log line or a repr by accident,
    and read from the secret provider (``secret:`` in ``config.yaml``) rather
    than written into the file — the same route ``DatabaseConfig.encryption_key``
    takes.
    """

    api_base_url: str = GRAPH_API_BASE_URL
    """Root of the Graph REST API, without a trailing slash.

    Also the fence around a stored cursor. Graph's ``nextLink`` and
    ``deltaLink`` are whole URLs and this adapter follows them with a bearer
    token attached, so
    :func:`~mailarc_m365.source.mapping.read_cursor_url` refuses any that does
    not share this origin.
    """

    authority_host: str = MICROSOFT_AUTHORITY_HOST
    """Where MSAL mints tokens, without a trailing slash or a tenant."""

    default_tenant: str = COMMON_TENANT
    """The authority a delegated account uses when its own field is empty.

    ``common`` accepts work, school and personal accounts, which is what a
    desktop archive wants. A deployment that serves exactly one organisation
    sets its tenant here and every mailbox added afterwards inherits it.
    """

    delta_folder: str = "allitems"
    """The folder Graph's message delta runs over.

    Graph has **no mailbox-wide message delta**. Every documented URL for
    ``messages/delta`` carries a ``mailFolders/{id}`` segment, so a delta is
    always a delta *of one folder* — which is why this setting exists at all
    and why it is not a hard-coded constant.

    ``allitems`` is the search folder that spans the whole mailbox, and it is
    the only well-known name that makes the delta mean what the port's
    ``watermark()`` says it means. It is also the one thing in this file most
    likely to need changing: some mailboxes — notably older hybrid and
    single-tenant configurations — answer ``ErrorItemNotFound: The specified
    object was not found in the store., Default folder AllItems not found``.
    That refusal is a 404 and reaches the operator as one, naming this path,
    rather than being swallowed into a delta that silently covers nothing.
    Such a deployment sets this to ``inbox``, or to a folder id, and accepts
    that mail filed elsewhere by a server-side rule is picked up by the next
    full import rather than by a delta.
    """

    loopback_port: int = 0
    """Port the delegated consent's local redirect server binds.

    Zero lets the operating system pick a free one, which is what a desktop
    application wants: a fixed port is a collision waiting for the second
    window. Entra accepts any loopback port for a public client registered with
    ``http://localhost``, so nothing has to be registered up front.
    """

    consent_timeout: float = 300.0
    """Seconds the delegated consent waits for the browser to come back.

    A human at a consent screen takes as long as they take, but an abandoned
    tab must not leave a listener and a blocked thread behind forever. Five
    minutes is long for a login and short for a leak.
    """

    request_timeout: float = 60.0
    """Seconds any single Graph call may take before it counts as transient.

    Longer than Gmail's thirty because ``$value`` returns the *whole* MIME
    message, attachments included, in one response body — over someone's home
    connection a fifty-megabyte mail is a minute. Finite either way, so a hung
    socket becomes a retry instead of a stuck worker.
    """

    token_timeout: float = 30.0
    """Seconds MSAL may spend at the token endpoint. Not an API call."""

    page_size: int = 100
    """Message references per listing call.

    Graph's own ceiling for ``$top`` on messages is 1000. A hundred keeps one
    page cheap enough that a cancelled import stops promptly, and listing is
    not the expensive half.
    """

    watermark_page_size: int = 500
    """References per page while
    :meth:`~mailarc_m365.source.source.M365Source.watermark` drains a delta.

    Larger than :attr:`page_size` because nothing consumes these pages — the
    drain exists only to reach the ``deltaLink`` at the end of the chain, and
    every page is one round trip closer to it. Graph treats
    ``Prefer: odata.maxpagesize`` as a ceiling it may undercut, so a value it
    dislikes costs pages, never correctness.
    """

    watermark_max_pages: int = 200
    """How far that drain may walk before it settles for where it got to.

    A bound rather than a loop, because an unbounded one against a mailbox that
    keeps growing is a worker that never returns. Reaching it is not a failure:
    the drain hands back the ``nextLink`` it stopped at, which is a legal
    incremental cursor — the next run resumes the same enumeration from there
    and gets a little closer to the end. See
    :meth:`~mailarc_m365.source.source.M365Source.watermark`.
    """

    def configured(self) -> bool:
        """Whether this installation has an Entra application to speak as.

        Asked before a consent is offered, so an operator who has not set the
        registration up gets a sentence instead of a browser window opening on
        a Microsoft error page.

        Only the client id, because that is all a delegated sign-in needs and a
        delegated sign-in is what a desktop installation does. App-only asks
        for the secret separately, in
        :meth:`app_client_secret`, at the point where its absence is
        actionable.

        A value still wearing its ``<placeholders>`` counts as unset: the
        ``secret:`` references in ``config.yaml`` are resolved eagerly and a
        missing key fails the whole startup, so the shipped defaults have to be
        *present* rather than absent, and present is not the same as set.
        """
        return _is_set(self.client_id)

    def app_client_secret(self) -> str:
        """The secret as MSAL wants it: unwrapped, at the last moment.

        Empty when this installation has none, which is the ordinary state of a
        desktop archive and the reason app-only says so with its own sentence
        rather than letting Entra answer ``AADSTS7000215``.
        """
        secret = self.client_secret.get_secret_value() if self.client_secret else ""
        return secret if _is_set(secret) else ""

    def authority_for(self, tenant: str) -> str:
        """The MSAL authority URL for one tenant.

        MSAL takes the tenant as part of the authority rather than as an
        argument, so this is where the two halves meet — and the only place
        that knows the shape, which keeps a stray slash from becoming a
        ``ValueError`` inside the library.
        """
        named = tenant.strip().strip("/").strip() or COMMON_TENANT
        return f"{self.authority_host.rstrip('/')}/{named}"


def _is_set(value: str) -> bool:
    """Whether a configured string is a real value rather than a placeholder."""
    stripped = value.strip()
    return bool(stripped) and not (stripped.startswith("<") and stripped.endswith(">"))
