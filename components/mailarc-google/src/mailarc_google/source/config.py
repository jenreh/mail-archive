"""Where Gmail is, which OAuth client speaks for us, and how hard we may lean.

Nothing here names a mailbox. Which account, whose refresh token and how far
the last run got are **state in SQLite** (§8.1) — a second Gmail account must
not need a second config.

The OAuth client is the opposite: it belongs to the *installation*, not to any
one mailbox. Every account this deployment connects goes through the same
registered client, so it is configured once by whoever runs the application
and no user is ever asked to make a Google Cloud project. That is also why it
lives here rather than on
:class:`~mailarc_google.source.credentials.GmailCredentials`: two copies of a
client secret drift the day somebody rotates it, and the one in the database
would go on being used until every account broke.

The two URLs are settings rather than constants for one reason: a test has to
be able to point them at a local HTTP server. The phase 3 DoD is explicit that
no test talks to Google.
"""

from appkit_commons.configuration.base import BaseConfig
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from mailarc_google.source.model import GMAIL_API_BASE_URL, GOOGLE_TOKEN_URI


class GmailConfig(BaseConfig):
    """The OAuth client, the endpoints, the timeouts and the page size."""

    model_config = SettingsConfigDict(
        env_prefix="app_google_",
        env_file=".env",
        populate_by_name=True,
    )

    client_id: str = ""
    """The desktop OAuth client this installation speaks as.

    Empty by default because there is no sensible one to ship: the client is
    whoever deploys this, and the consent screen and the API quota are theirs.
    Empty simply means Gmail is not set up, and
    :meth:`configured` is how the application asks.
    """

    client_secret: SecretStr | None = None
    """The secret belonging to :attr:`client_id`.

    A ``SecretStr`` so it cannot fall into a log line or a repr by accident,
    and read from the secret provider (``secret:`` in ``config.yaml``) rather
    than written into the file — the same route
    ``DatabaseConfig.encryption_key`` takes.
    """

    api_base_url: str = GMAIL_API_BASE_URL
    """Root of the Gmail REST API, without a trailing slash."""

    token_uri: str = GOOGLE_TOKEN_URI
    """Where an access token is minted and refreshed."""

    loopback_port: int = 0
    """Port the consent flow's local redirect server binds.

    Zero lets the operating system pick a free one, which is what a desktop
    application wants: a fixed port is a collision waiting for the second
    window. Google accepts any loopback port for an installed-app client, so
    nothing has to be registered up front.
    """

    consent_timeout: float = 300.0
    """Seconds the consent flow waits for the browser to come back.

    A human at a consent screen takes as long as they take, but an abandoned
    tab must not leave a listener and a blocked thread behind forever — which
    is what the library's own flow did. Five minutes is long for a login and
    short for a leak.
    """

    request_timeout: float = 30.0
    """Seconds any single HTTP call may take before it counts as transient.

    Generous, because ``format=raw`` on a message with attachments is a large
    body over someone's home connection — but finite, so a hung socket becomes
    a retry instead of a stuck worker.
    """

    page_size: int = 100
    """Message references per listing call.

    Gmail's own maximum is 500; a hundred keeps one page cheap enough that a
    cancelled import stops promptly, and listing is not the expensive half.
    """

    def configured(self) -> bool:
        """Whether this installation has an OAuth client to speak as.

        Asked before a consent is offered, so an operator who has not set the
        client up gets a sentence instead of a browser window opening on a
        Google error page.

        ``.env.default`` ships both values as ``<placeholders>`` — the
        ``secret:`` references in ``config.yaml`` are resolved eagerly and a
        missing key fails the whole startup, so they have to be *present*
        rather than absent. Present is not the same as set, and a value still
        wearing its angle brackets counts as unset.
        """
        return _is_set(self.client_id) and _is_set(self.oauth_client_secret())

    def oauth_client_secret(self) -> str:
        """The secret as the token endpoint wants it: unwrapped, at the last moment."""
        return self.client_secret.get_secret_value() if self.client_secret else ""


def _is_set(value: str) -> bool:
    """Whether a configured string is a real value rather than a placeholder."""
    stripped = value.strip()
    return bool(stripped) and not (stripped.startswith("<") and stripped.endswith(">"))
