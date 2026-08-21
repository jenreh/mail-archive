"""`GmailConfig` is five settings; the prefix is the part that can silently rot.

A typo in `env_prefix` costs nothing at import time and everything at run time
— the setting is simply never read and the default quietly wins. The two URLs
matter twice over: they are also the seam that keeps the phase 3 DoD honest, so
a test can point the adapter at a local server instead of at Google.
"""

from mailarc_google.source.config import GmailConfig
from mailarc_google.source.model import GMAIL_API_BASE_URL, GOOGLE_TOKEN_URI

FAKE_API_ROOT = "http://127.0.0.1:9/gmail/v1"
FAKE_OAUTH_URL = "http://127.0.0.1:9/token"


def test_the_defaults_point_at_google() -> None:
    config = GmailConfig()

    assert config.api_base_url == GMAIL_API_BASE_URL
    assert config.token_uri == GOOGLE_TOKEN_URI


def test_the_api_root_has_no_trailing_slash() -> None:
    """Paths are joined onto it; a trailing slash makes every URL doubled."""
    assert not GmailConfig().api_base_url.endswith("/")


def test_the_loopback_port_is_chosen_by_the_operating_system() -> None:
    """A fixed port is a collision waiting for the second window."""
    assert GmailConfig().loopback_port == 0


def test_a_page_stays_well_under_gmails_own_maximum() -> None:
    """Gmail allows 500; a smaller page is what makes a cancel prompt."""
    config = GmailConfig()

    assert 0 < config.page_size <= 500
    assert config.request_timeout > 0


def test_the_environment_prefix_is_app_google(monkeypatch) -> None:
    monkeypatch.setenv("app_google_page_size", "25")
    monkeypatch.setenv("app_google_loopback_port", "8123")

    config = GmailConfig()

    assert config.page_size == 25
    assert config.loopback_port == 8123


def test_the_endpoints_can_be_pointed_at_a_local_server(monkeypatch) -> None:
    """This is the setting the whole offline test suite rests on."""
    monkeypatch.setenv("app_google_api_base_url", FAKE_API_ROOT)
    monkeypatch.setenv("app_google_token_uri", FAKE_OAUTH_URL)

    config = GmailConfig()

    assert config.api_base_url == FAKE_API_ROOT
    assert config.token_uri == FAKE_OAUTH_URL


def test_an_explicit_value_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("app_google_page_size", "25")

    assert GmailConfig(page_size=10).page_size == 10


class TestTheInstallationsOAuthClient:
    """One registered client speaks for every mailbox this deployment archives.

    It is configuration and not a credential column: a copy per account would
    still be in use the day somebody rotates the secret, and every account
    would break one refresh later with nothing pointing at the cause.
    """

    def test_it_is_unset_by_default(self) -> None:
        """There is no sensible client to ship — it belongs to the deployer."""
        config = GmailConfig(client_id="", client_secret=None)

        assert config.configured() is False
        assert config.oauth_client_secret() == ""

    def test_both_halves_are_needed(self) -> None:
        assert not GmailConfig(client_id="a", client_secret=None).configured()
        assert not GmailConfig(client_id="", client_secret="b").configured()
        assert GmailConfig(client_id="a", client_secret="b").configured()

    def test_the_placeholder_env_default_ships_does_not_count_as_set(self) -> None:
        """`config.yaml` resolves both through `secret:`, and a missing key
        fails the whole startup — so `.env.default` has to ship *something*.
        What it ships is angle brackets, and those are not a client."""
        config = GmailConfig(
            client_id="<your google oauth client id here>",
            client_secret="<your google oauth client secret here>",
        )

        assert config.configured() is False

    def test_the_secret_stays_wrapped_until_it_is_needed(self) -> None:
        """A `SecretStr` cannot fall into a log line or a repr by accident."""
        config = GmailConfig(client_id="a", client_secret="GOCSPX-real")

        assert "GOCSPX-real" not in repr(config)
        assert "GOCSPX-real" not in str(config.client_secret)
        assert config.oauth_client_secret() == "GOCSPX-real"

    def test_the_environment_can_supply_it(self, monkeypatch) -> None:
        """`app_google_*`, the same override every other config takes."""
        monkeypatch.setenv("app_google_client_id", "from-the-environment")
        monkeypatch.setenv("app_google_client_secret", "shh")

        config = GmailConfig()

        assert config.client_id == "from-the-environment"
        assert config.configured() is True
