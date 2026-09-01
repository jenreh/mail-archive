"""What the configuration promises, including the two places it must not lie.

`configured()` is what stands between an operator who has not registered an
Entra application and a browser window opening on a Microsoft error page, and
it has to treat a shipped `<placeholder>` as unset — `config.yaml` resolves its
`secret:` references eagerly, so the defaults are present rather than absent.
"""

import pytest
from pydantic import SecretStr

from mailarc_m365.source.config import M365Config
from mailarc_m365.source.model import COMMON_TENANT


class TestConfigured:
    def test_an_installation_without_a_client_id_is_not_set_up(self) -> None:
        assert M365Config(client_id="").configured() is False

    def test_a_placeholder_counts_as_unset(self) -> None:
        assert M365Config(client_id="<your-entra-client-id>").configured() is False

    def test_a_client_id_alone_is_enough_for_a_delegated_sign_in(self) -> None:
        # A public client must NOT have a secret; requiring one here would make
        # every desktop installation report itself unconfigured.
        assert M365Config(client_id="a-real-guid").configured() is True


class TestTheClientSecret:
    def test_an_unset_secret_is_an_empty_string_not_a_none_to_unwrap(self) -> None:
        assert M365Config(client_id="x").app_client_secret() == ""

    def test_a_placeholder_secret_is_also_empty(self) -> None:
        config = M365Config(client_id="x", client_secret=SecretStr("<secret>"))
        assert config.app_client_secret() == ""

    def test_a_real_secret_is_unwrapped_only_when_asked_for(self) -> None:
        config = M365Config(client_id="x", client_secret=SecretStr("shhh"))
        assert config.app_client_secret() == "shhh"
        assert "shhh" not in repr(config)


class TestTheAuthority:
    def test_a_tenant_is_joined_onto_the_host_with_one_slash(self) -> None:
        config = M365Config(authority_host="https://login.example.test/")
        assert (
            config.authority_for("contoso.onmicrosoft.com")
            == "https://login.example.test/contoso.onmicrosoft.com"
        )

    @pytest.mark.parametrize("tenant", ["", "   ", "/"])
    def test_an_empty_tenant_falls_back_to_the_multi_tenant_authority(
        self, tenant: str
    ) -> None:
        # MSAL raises on an authority that is one slash long, and a ValueError
        # from inside a library is not an error a user can act on.
        config = M365Config(authority_host="https://login.example.test")
        assert config.authority_for(tenant).endswith(f"/{COMMON_TENANT}")


def test_the_config_names_no_mailbox() -> None:
    """A second Microsoft 365 account must not need a second config.

    `delta_folder` is the apparent exception and is not one: it names a folder
    that exists inside *every* mailbox, not a mailbox.
    """
    fields = set(M365Config.model_fields)
    assert not {"mailbox", "user_id", "email_address", "address"} & fields
