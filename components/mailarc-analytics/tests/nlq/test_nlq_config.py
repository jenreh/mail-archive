"""The settings, and the one default that is the whole feature.

``provider`` defaults to ``none`` for the reason ``SemanticConfig``'s does:
§7.4 keeps the desktop application free of prerequisites, so an archive with no
interpreter has to be a complete installation rather than a broken one. What is
asserted here is that the default really is off, that the block is read under
its own ``app_llm_`` prefix rather than the embedder's, and that nothing about
this configuration can turn a missing setting into a silent request.
"""

import pytest
from pydantic import SecretStr

from mailarc_analytics.nlq.chat import DEFAULT_BASE_URLS, DEFAULT_MODELS
from mailarc_analytics.nlq.config import LlmConfig, LlmProvider


class TestTheDefault:
    def test_no_interpreter_is_the_default(self) -> None:
        assert LlmConfig().provider is LlmProvider.NONE

    def test_nothing_else_is_set_either(self) -> None:
        """Every per-provider value falls through to the table in ``chat``, so
        one shared default cannot be wrong for the provider that did not get
        picked — ``localhost:11434`` pointed at OpenAI is a connection error."""
        config = LlmConfig()
        assert config.model == ""
        assert config.base_url == ""
        assert config.api_key is None

    def test_the_temperature_starts_at_zero(self) -> None:
        """The same question should produce the same filter. A search that
        quietly returns a different set each time is worse than a wrong one."""
        assert LlmConfig().temperature == 0.0


class TestItsOwnBlock:
    def test_it_reads_its_own_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("app_llm_provider", "ollama")
        assert LlmConfig().provider is LlmProvider.OLLAMA

    def test_it_does_not_read_the_embedders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two models differ in what leaves the machine, so an installation
        may reasonably want either without the other — which a shared setting
        could not express."""
        monkeypatch.setenv("app_semantic_provider", "openai")
        assert LlmConfig().provider is LlmProvider.NONE

    def test_a_key_is_a_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So that a configuration repr in a log or a traceback cannot carry
        it — the trap ``mail_credentials`` records for encrypted columns."""
        monkeypatch.setenv("app_llm_api_key", "sk-not-in-a-log")
        config = LlmConfig()
        assert isinstance(config.api_key, SecretStr)
        assert "sk-not-in-a-log" not in repr(config)


class TestTheProviderTables:
    def test_every_provider_that_can_be_built_has_an_entry(self) -> None:
        """A missing entry is a ``KeyError`` at construction, which is exactly
        where ``build_chat_client`` promises never to raise."""
        buildable = set(LlmProvider) - {LlmProvider.NONE}
        assert set(DEFAULT_BASE_URLS) == buildable
        assert set(DEFAULT_MODELS) == buildable

    def test_azure_has_no_default_of_either_kind(self) -> None:
        """Both would have to name one customer's resource and deployment.
        Empty is refused by name at the first call rather than sent nowhere."""
        assert DEFAULT_BASE_URLS[LlmProvider.AZURE_OPENAI] == ""
        assert DEFAULT_MODELS[LlmProvider.AZURE_OPENAI] == ""

    def test_the_local_provider_needs_no_account(self) -> None:
        """The one a desktop installation can actually turn on, which is why
        the documentation leads with it."""
        assert DEFAULT_BASE_URLS[LlmProvider.OLLAMA].startswith("http://localhost")
