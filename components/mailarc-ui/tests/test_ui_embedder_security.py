"""The discipline this form exists under: the API key is write-only.

Separated from :mod:`test_ui_embedder_state` along a seam rather than at a line
number. That file is about what the form *does*; this one is about what it must
never do, and the two are read by different people for different reasons — one
by somebody changing the form, the other by somebody auditing it. The world both
run in is :mod:`embedder_form`.

Not "the component does not render it" — the state never holds it. A key is
stored, the form is loaded, saved and loaded again, and the plaintext appears
in no var the browser could ever be sent, and in no log line either. The last
test closes the other way in: ``setvar`` reaches a var by name over the socket,
through whatever setter happens to exist.
"""

import logging

import pytest
import reflex as rx
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.registry import service_registry
from embedder_form import (
    SECRET,
    clear_key,
    composition,
    configured,
    everything_the_browser_would_see,
    keyed,
    load_form,
    save_form,
    searching,
    sessions,
    state,
    stored_row,
)
from reflex.event import EventHandler
from sqlalchemy import text

from mailarc_analytics.semantic import SemanticProvider
from mailarc_ui.embedder import (
    KEY_CLEARED,
    KEY_NOT_STORED,
    EmbedderSettingsState,
)

__all__ = ["composition", "configured", "keyed", "searching", "sessions", "state"]
"""pytest collects a fixture off the importing module's namespace, so the six
are imported to be used; ``__all__`` is what stops ruff removing them again —
the same device ``test_ui_insights_rebuild.py`` uses for the same reason."""


class TestTheWriteOnlyApiKey:
    """The key goes in and never comes back — asserted, not asserted about."""

    async def test_a_typed_key_is_stored_encrypted(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        await state.set_provider(SemanticProvider.OPENAI.value)
        await state.set_api_key(SECRET)

        await save_form(state)

        assert state.api_key_stored is True
        async with sessions() as session:
            raw = await session.execute(
                text("SELECT api_key FROM semantic_settings WHERE id = 1")
            )
            ciphertext = raw.scalar_one()
        assert SECRET not in str(ciphertext)

    async def test_a_saved_then_reloaded_form_never_carries_the_secret(
        self, state, sessions, searching
    ) -> None:
        """The claim in one line: it is not in anything the browser is sent."""
        await load_form(state)
        await state.set_api_key(SECRET)
        await save_form(state)

        await load_form(state)

        assert SECRET not in everything_the_browser_would_see(state)
        assert state._typed_key == ""
        assert state.api_key_stored is True
        assert "A key is stored here" in state.key_status, (
            "the badge distinguishes a key stored here — the only one Clear "
            "can forget — from one the configuration file supplies"
        )

    async def test_the_typed_key_is_never_a_var_the_browser_is_sent(
        self, state, sessions, searching
    ) -> None:
        """Even mid-typing, before any save has had a chance to clear it."""
        await load_form(state)

        await state.set_api_key(SECRET)

        assert "_typed_key" not in EmbedderSettingsState.vars
        assert "_typed_key" in EmbedderSettingsState.backend_vars
        assert SECRET not in everything_the_browser_would_see(state)
        assert state.key_pending is True

    async def test_an_empty_box_leaves_the_stored_key_alone(
        self, state, sessions, searching
    ) -> None:
        """The rule an ordinary form gets wrong, and the store cannot."""
        await load_form(state)
        await state.set_api_key(SECRET)
        await save_form(state)

        await load_form(state)
        await state.set_model("text-embedding-3-small")
        await save_form(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert stored.api_key == SECRET
        assert stored.model == "text-embedding-3-small"

    async def test_whitespace_is_not_a_key(self, state, sessions, searching) -> None:
        await load_form(state)
        await state.set_api_key(SECRET)
        await save_form(state)

        await load_form(state)
        await state.set_api_key("   ")
        await save_form(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert stored.api_key == SECRET

    async def test_clearing_is_its_own_control(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        await state.set_api_key(SECRET)
        await save_form(state)

        await clear_key(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert stored.api_key is None
        assert state.api_key_stored is False
        assert state.notice == KEY_CLEARED

    async def test_a_key_that_cannot_be_encrypted_rolls_the_whole_save_back(
        self, state, sessions, searching, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One transaction, so "nothing was written" is true when it says so.

        And the message is the fixed one: ``StatementError`` renders its bind
        parameters, and the bind parameter of this write is the key.
        """
        await load_form(state)
        await state.set_provider(SemanticProvider.OPENAI.value)
        await state.set_api_key(SECRET)
        service_registry().register_as(
            DatabaseConfig,
            DatabaseConfig.model_validate({"encryption_key": "nonsense"}),
        )

        await save_form(state)

        assert state.error == KEY_NOT_STORED
        assert SECRET not in state.error
        assert await stored_row(sessions) is None

    async def test_a_failing_key_write_is_logged_without_the_key(
        self, state, sessions, searching, caplog
    ) -> None:
        """The one way this write fails is also the one way the key can leak.

        ``StatementError`` renders its bind parameters and the bind parameter
        here is the plaintext key, so a caller doing the right thing — logging
        the failure — is what would leak it. ``ApiKeyNotStored`` carries the
        cause stripped, and this asserts it over everything the run logged
        rather than over the one call that raised.
        """
        await load_form(state)
        await state.set_api_key(SECRET)
        service_registry().register_as(
            DatabaseConfig, DatabaseConfig.model_validate({"encryption_key": "nope"})
        )

        with caplog.at_level(logging.DEBUG):
            await save_form(state)

        assert caplog.records, "nothing was logged, so nothing was checked"
        assert SECRET not in caplog.text
        assert "could not be stored" in caplog.text

    def test_setvar_cannot_reach_what_the_key_is_stored_behind(self) -> None:
        """``setvar`` sets a var by name, through whatever setter exists.

        ``State.setvar(name, value)`` is generated for every state and is
        addressable over the socket like any other event; what it does is call
        ``set_<name>``. Reflex 0.9 auto-generates no setters, so on this class
        the only vars it can reach are the four the form actually edits, and
        everything else raises ``AttributeError`` — ``api_key_stored`` cannot
        be set from outside at all, so nothing over the socket can make the
        form claim a key it does not have. That is a property of *this* class
        and not of Reflex, so it is asserted here: adding a plain
        ``set_api_key_stored`` later would open it and nothing else would
        notice.

        Reflex's own inherited vars are left out. ``is_hydrated`` has a setter
        the framework needs, and it says nothing about the embedder.
        """
        own = set(EmbedderSettingsState.vars) - set(rx.State.vars)
        reachable = [
            name
            for name in own
            if isinstance(
                getattr(EmbedderSettingsState, f"set_{name}", None), EventHandler
            )
        ]

        assert sorted(reachable) == ["base_url", "dimension", "model", "provider"]
