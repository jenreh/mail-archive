"""The two disciplines this form exists under: a write-only key, and the gate.

Separated from :mod:`test_ui_embedder_state` along a seam rather than at a line
number. That file is about what the form *does*; this one is about what it must
never do, and the two are read by different people for different reasons — one
by somebody changing the form, the other by somebody auditing it. The world both
run in is :mod:`embedder_form`.

**The key is write-only.** Not "the component does not render it" — the state
never holds it. A key is stored, the form is loaded, saved and loaded again, and
the plaintext appears in no var the browser could ever be sent, and in no log
line either.

**Every handler is gated.** The list is discovered from the class rather than
written down, so a handler added next year is covered by a test written today;
and ``setvar``, which reaches a var by name over the socket, is checked
separately because it routes through whatever setter happens to exist.
"""

import inspect
import logging
from typing import Any

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
    reset_form,
    save_form,
    searching,
    sessions,
    state,
    stored_row,
)
from insights_archive import FakeUser, signed_in_as
from reflex.event import EventHandler
from sqlalchemy import text

from mailarc_analytics.semantic import SemanticProvider
from mailarc_ui.embedder import (
    KEY_CLEARED,
    KEY_NOT_STORED,
    NOT_ALLOWED,
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

    async def test_a_refused_caller_is_shown_nothing_at_all(
        self, sessions, searching, composition, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Including any reading an administrator left in the same session."""
        admin = EmbedderSettingsState()
        signed_in_as(admin, FakeUser(is_admin=True), monkeypatch)
        await load_form(admin)
        await admin.set_api_key(SECRET)
        signed_in_as(admin, FakeUser(is_admin=False), monkeypatch)

        await load_form(admin)

        assert admin.allowed is False
        assert admin.error == NOT_ALLOWED
        assert admin._typed_key == ""
        assert SECRET not in everything_the_browser_would_see(admin)


class TestEveryHandlerIsGated:
    """The gate is per-handler by construction, and per-handler rules rot.

    ``admin_only=True`` on the page is a render-time ``rx.cond``; appkit puts
    ``check_auth`` in front of the ``on_load`` chain and Reflex runs the rest of
    it whatever that returns; and a Reflex event is addressable by name over the
    websocket. So the rule is asserted structurally — over a list discovered
    from the class, not one written down here — and the two ways around it are
    closed separately.
    """

    IGNORED = {"setvar", "stop_polling"}
    """The two handlers this rule does not reach, and why.

    ``setvar`` is Reflex's own generic setter and has its own test below.
    ``stop_polling`` clears one flag in the caller's *own* session state, reads
    nothing and writes nothing outside it; a gate that could refuse it would
    leave a background task polling the database for the life of that session,
    which is the fault it exists to prevent rather than one it could cause. It
    is asserted separately in ``TestTheRebuildControlRevealsNothing``.
    """

    def _handlers(self) -> dict[str, Any]:
        return {
            name: value.fn
            for name, value in vars(EmbedderSettingsState).items()
            if isinstance(value, EventHandler) and name not in self.IGNORED
        }

    def test_the_discovery_finds_the_handlers_this_state_actually_has(self) -> None:
        """So the check below cannot pass by finding nothing."""
        assert set(self._handlers()) == {
            "load",
            "save",
            "clear_api_key",
            "use_configuration_file",
            "set_provider",
            "set_model",
            "set_dimension",
            "set_base_url",
            "set_api_key",
            "start_embed",
            "cancel_embed",
            "rebuild_index",
            "poll",
        }

    def test_every_one_of_them_consults_the_gate(self) -> None:
        ungated = [
            name
            for name, fn in self._handlers().items()
            if "_may_configure" not in inspect.getsource(fn)
        ]

        assert not ungated, (
            f"{ungated} change or read the embedder configuration without "
            "consulting _may_configure. A Reflex handler is reachable by name "
            "over the socket, so the page's admin_only decorator does not "
            "cover it."
        )

    def test_setvar_cannot_route_around_the_gate(self) -> None:
        """``setvar`` sets a var by name, through whatever setter exists.

        ``State.setvar(name, value)`` is generated for every state and is
        addressable over the socket like any other event; what it does is call
        ``set_<name>``. Reflex 0.9 auto-generates no setters, so on this class
        the only vars it can reach are the four this state wrote gated handlers
        for and everything else raises ``AttributeError`` — a var like
        ``allowed`` or ``api_key_stored`` cannot be set from outside at all.
        That is a property of *this* class, not of Reflex, so it is asserted
        here: adding a plain ``set_allowed`` later would open the gate and
        nothing else would notice.

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
        assert not [
            name
            for name in reachable
            if "_may_configure"
            not in inspect.getsource(getattr(EmbedderSettingsState, f"set_{name}").fn)
        ]

    async def test_a_non_admin_load_reads_nothing_and_says_so(
        self, sessions, searching, composition, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refused = EmbedderSettingsState()
        signed_in_as(refused, FakeUser(is_admin=False), monkeypatch)

        await load_form(refused)

        assert refused.error == NOT_ALLOWED
        assert refused.allowed is False
        assert searching.asked == 0

    async def test_a_non_admin_save_writes_nothing(
        self, sessions, searching, composition, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refused = EmbedderSettingsState()
        signed_in_as(refused, FakeUser(is_admin=False), monkeypatch)
        refused.provider = SemanticProvider.OPENAI.value

        await save_form(refused)

        assert await stored_row(sessions) is None
        assert composition.reloads == 0
        assert refused.error == NOT_ALLOWED

    async def test_a_non_admin_cannot_clear_a_key_or_reset_the_settings(
        self, state, sessions, searching, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await load_form(state)
        await state.set_api_key(SECRET)
        await save_form(state)
        signed_in_as(state, FakeUser(is_admin=False), monkeypatch)

        await clear_key(state)
        await reset_form(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert stored.api_key == SECRET

    async def test_a_session_nobody_can_identify_is_refused(
        self, state, sessions, searching, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails closed: "cannot tell" is not "yes" for a form that writes."""

        async def broken(_self: object) -> object:
            raise RuntimeError("no event context")

        monkeypatch.setattr(EmbedderSettingsState, "_current_user", broken)

        await load_form(state)

        assert state.allowed is False
        assert state.error == NOT_ALLOWED

    async def test_a_logged_out_session_is_refused(
        self, state, sessions, searching, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signed_in_as(state, None, monkeypatch)

        await load_form(state)

        assert state.allowed is False


@pytest.mark.parametrize(
    ("handler", "argument", "field"),
    [
        ("set_provider", SemanticProvider.OLLAMA.value, "provider"),
        ("set_model", "nomic-embed-text", "model"),
        ("set_dimension", 1536, "dimension"),
        ("set_base_url", "http://elsewhere", "base_url"),
    ],
)
@pytest.mark.usefixtures("searching")
async def test_a_refused_caller_cannot_move_a_field(
    monkeypatch, handler, argument, field
) -> None:
    """Every setter refuses, and refusing means the var did not move.

    Together with ``test_setvar_cannot_route_around_the_gate`` this closes the
    setters completely: there is no path from the socket to one of these four
    vars that does not pass the gate.
    """
    refused = EmbedderSettingsState()
    signed_in_as(refused, FakeUser(is_admin=False), monkeypatch)
    before = getattr(refused, field)

    await getattr(refused, handler)(argument)

    assert getattr(refused, field) == before
    assert refused.error == NOT_ALLOWED


@pytest.mark.usefixtures("searching")
async def test_a_refused_caller_cannot_leave_a_key_in_the_state(monkeypatch) -> None:
    """``set_api_key`` is the setter where refusing actually matters."""
    refused = EmbedderSettingsState()
    signed_in_as(refused, FakeUser(is_admin=False), monkeypatch)

    await refused.set_api_key(SECRET)

    assert refused._typed_key == ""
    assert refused.key_pending is False
    assert refused.error == NOT_ALLOWED
