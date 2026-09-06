"""The second half of the models page: the model that reads a question.

The embedder form's tests answer "what does changing this cost". This form has
no such question — nothing it produces is stored, so a change costs the next
question — and what is left is the three properties that *are* the same, and
which are the ones worth breaking:

**A key goes in and never comes back.** It is held in a backend-only var, it is
written through a repository whose ``store`` has no parameter for it, and the
one way that write can fail is guarded so the plaintext does not reach a log.

**The stored row wins, and can be given back.** A save merges over the file;
**Use the configuration file** writes nulls and drops the key, after which the
archive is exactly where it was before anybody opened the page.

**Two administrators cannot silently overwrite each other.** The row's
timestamp is the baseline, and a save from a form somebody else has moved on
from is refused rather than applied.

The merge in :class:`~embedder_form.InterpreterComposition` is the real one —
``LlmOverrides.applied_to`` — so a test can follow a save all the way to what
the archive would then build a client from.
"""

from datetime import timedelta

import pytest
from embedder_form import (
    SECRET,
    Sessions,
    drive_interpreter,
    interpreter_state,
    keyed,
    llm_composition,
    llm_configured,
    sessions,
)

# The fixtures live in a shared module rather than a conftest — see
# `embedder_form`'s docstring for why — so pytest only finds them where they
# are imported by name.
__all__ = [
    "interpreter_state",
    "keyed",
    "llm_composition",
    "llm_configured",
    "sessions",
]

from mailarc_analytics.nlq import LlmProvider
from mailarc_core.database.entities import LLM_SETTINGS_ID, LlmSettingsEntity
from mailarc_core.database.repositories import LlmSettingsRepository
from mailarc_ui.embedder.interpreter import (
    BAD_BASE_URL,
    BASE_URL_FIELD,
    PROVIDER_OPTIONS,
    SETTINGS_MOVED,
    InterpreterSettingsState,
)

pytestmark = pytest.mark.usefixtures("llm_composition")


async def llm_row(sessions: Sessions) -> LlmSettingsEntity | None:
    async with sessions() as session:
        return await session.get(LlmSettingsEntity, LLM_SETTINGS_ID)


class TestOpeningTheForm:
    async def test_a_fresh_installation_reads_the_defaults(
        self, interpreter_state
    ) -> None:
        """Off is a complete state, not a broken one — so nothing is stored,
        nothing is wrong, and the form says so with no message at all."""
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)

        assert interpreter_state.provider == LlmProvider.NONE.value
        assert interpreter_state.interpreter_configured is False
        assert interpreter_state.error == ""
        assert interpreter_state.loading is False

    async def test_it_reads_back_what_was_stored(
        self, interpreter_state, sessions
    ) -> None:
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)
        interpreter_state.set_model("qwen2.5:7b-instruct")
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        fresh = InterpreterSettingsState()
        await drive_interpreter(InterpreterSettingsState.load, fresh)

        assert fresh.provider == LlmProvider.OLLAMA.value
        assert fresh.model == "qwen2.5:7b-instruct"
        assert fresh.interpreter_configured is True

    async def test_a_provider_nobody_offered_is_refused(
        self, interpreter_state
    ) -> None:
        """The value arrives over a socket. One nobody offered would be stored
        and then refused at the first question, which is a fault a long way
        from where it was caused."""
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)

        interpreter_state.set_provider("telepathy")

        assert interpreter_state.provider == LlmProvider.NONE.value
        assert "telepathy" not in {one["value"] for one in PROVIDER_OPTIONS}


class TestTheKeyNeverComesBack:
    async def test_a_typed_key_is_not_in_anything_the_browser_gets(
        self, interpreter_state
    ) -> None:
        """The whole rule, checked the blunt way: a substring search over
        everything this state would serialise."""
        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        interpreter_state.set_api_key(SECRET)

        assert SECRET not in str(interpreter_state.dict())

    async def test_saving_stores_it_and_forgets_the_typed_one(
        self, interpreter_state, sessions
    ) -> None:
        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        interpreter_state.set_api_key(SECRET)

        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        row = await llm_row(sessions)
        assert row is not None
        assert row.api_key == SECRET
        assert interpreter_state.api_key_stored is True
        assert interpreter_state.key_pending is False

    async def test_an_empty_box_keeps_the_stored_key(
        self, interpreter_state, sessions
    ) -> None:
        """ "Unchanged" is the only thing an empty box can mean, and it is the
        repository that makes it so — ``store`` has no key parameter at all."""
        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        interpreter_state.set_api_key(SECRET)
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        interpreter_state.set_model("gpt-4o-mini")
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        row = await llm_row(sessions)
        assert row is not None
        assert row.api_key == SECRET
        assert row.model == "gpt-4o-mini"

    async def test_clearing_it_is_its_own_deliberate_act(
        self, interpreter_state, sessions
    ) -> None:
        """Without this button there is no way back from "a key is stored"
        except typing another one — and no way at all to stop sending a third
        party a credential that should have been revoked."""
        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        interpreter_state.set_api_key(SECRET)
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        await drive_interpreter(
            InterpreterSettingsState.clear_api_key, interpreter_state
        )

        row = await llm_row(sessions)
        assert row is not None
        assert row.api_key is None
        assert interpreter_state.api_key_stored is False

    async def test_a_key_from_the_file_cannot_be_cleared_from_here(
        self, interpreter_state
    ) -> None:
        """It is not this page's to remove, and a button that looked like it
        could would be one that silently did nothing."""
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        interpreter_state.api_key_in_force = True

        assert interpreter_state.can_clear_key is False
        assert interpreter_state.key_status == "A key comes from the configuration"

    async def test_a_provider_that_needs_a_key_and_has_none_says_so(
        self, interpreter_state
    ) -> None:
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        interpreter_state.set_provider(LlmProvider.OPENAI.value)

        assert interpreter_state.key_missing is True

    async def test_the_local_provider_never_asks_for_one(
        self, interpreter_state
    ) -> None:
        """Ollama ignores a key entirely, so warning about a missing one would
        be warning about a setting that changes nothing."""
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)

        assert interpreter_state.key_matters is False
        assert interpreter_state.key_missing is False


class TestGivingItBackToTheFile:
    async def test_the_stored_row_wins_while_it_exists(
        self, interpreter_state, llm_composition
    ) -> None:
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)

        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        assert llm_composition.effective.provider is LlmProvider.OLLAMA

    async def test_using_the_file_puts_everything_back(
        self, interpreter_state, llm_composition, sessions
    ) -> None:
        """Without it a single save would be one-way: an administrator who
        configures the archive in ``config.yaml`` could never get back to it
        from the browser."""
        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        interpreter_state.set_model("gpt-4o-mini")
        interpreter_state.set_api_key(SECRET)
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        await drive_interpreter(
            InterpreterSettingsState.use_configuration_file, interpreter_state
        )

        row = await llm_row(sessions)
        assert row is not None
        assert (row.provider, row.model, row.base_url, row.api_key) == (
            None,
            None,
            None,
            None,
        )
        assert llm_composition.effective.provider is LlmProvider.NONE

    async def test_a_save_makes_the_running_application_re_read(
        self, interpreter_state, llm_composition
    ) -> None:
        """A form that wrote a setting nothing re-read would be a form that
        silently does nothing until the next restart."""
        before = llm_composition.reloads
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)

        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        assert llm_composition.reloads == before + 1

    async def test_a_save_that_lands_but_is_not_adopted_says_which(
        self, interpreter_state, llm_composition, sessions
    ) -> None:
        """The row is written either way, and "Saved" over an application still
        using the previous model would send somebody looking for the fault in
        the model."""
        llm_composition.breaks = True
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)

        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        row = await llm_row(sessions)
        assert row is not None
        assert row.provider == LlmProvider.OLLAMA.value
        assert "did not pick them up" in interpreter_state.error


async def _somebody_else_saved(sessions: Sessions, provider: str) -> None:
    """A write from another screen, a minute later.

    The minute is not decoration. ``updated`` is ``CURRENT_TIMESTAMP``, which
    on SQLite counts in whole seconds, so two writes inside one second carry
    the same value and the guard has nothing to tell them apart — as
    ``LlmSettingsRepository.store`` records. That is the *fast* race, closed by
    the ``saving`` flag; this test is about the slow one, so the clock is moved
    by hand rather than waited on.
    """
    async with sessions() as session:
        await LlmSettingsRepository().store(
            session, provider=provider, model="", base_url=""
        )
        row = await session.get(LlmSettingsEntity, LLM_SETTINGS_ID)
        assert row is not None
        row.updated = row.updated + timedelta(minutes=1)


class TestTwoAdministrators:
    async def test_a_save_from_a_stale_form_is_refused(
        self, interpreter_state, sessions
    ) -> None:
        """The slow race — two people at two screens, minutes apart — which had
        no guard at all before the baseline existed.

        A row has to exist before either form loads. With none, both baselines
        are ``None``, which honestly means "I read no row" and is what a fresh
        installation is — so the guard has nothing to compare and the second
        save is the first one there has been.
        """
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        # A save of this form's own first, so its baseline is a timestamp
        # rather than `None` — which honestly means "I read no row" and is what
        # a fresh installation is, so the guard has nothing to compare.
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)
        await _somebody_else_saved(sessions, LlmProvider.OLLAMA.value)

        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        row = await llm_row(sessions)
        assert row is not None
        assert row.provider == LlmProvider.OLLAMA.value
        assert interpreter_state.error == SETTINGS_MOVED
        # Its own sentence: the two forms save independently, so one
        # naming the wrong half would send a reader to re-check settings
        # nobody had touched.
        assert "interpreter" in interpreter_state.error

    async def test_the_refused_form_is_re_read_rather_than_left_stale(
        self, interpreter_state, sessions
    ) -> None:
        """Leaving the stale values would make the second press carry the
        baseline the refusal installed and succeed — quietly doing the damage
        the refusal just prevented."""
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        # A save of this form's own first, so its baseline is a timestamp
        # rather than `None` — which honestly means "I read no row" and is what
        # a fresh installation is, so the guard has nothing to compare.
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)
        await _somebody_else_saved(sessions, LlmProvider.OLLAMA.value)

        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        assert interpreter_state.provider == LlmProvider.OLLAMA.value


class TestTheBaseUrl:
    @pytest.mark.parametrize(
        "typed", ["gpu.internal:11434", "not a url", "ftp://host/x"]
    )
    async def test_something_that_is_not_an_http_url_is_refused(
        self, interpreter_state, sessions, typed: str
    ) -> None:
        """The value decides which host receives this archive's stored key on
        every question, and it arrives over a socket."""
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)
        interpreter_state.set_base_url(typed)

        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        assert interpreter_state.errors[BASE_URL_FIELD] == BAD_BASE_URL
        assert await llm_row(sessions) is None

    async def test_empty_is_the_providers_own_endpoint(self, interpreter_state) -> None:
        """A passing rule takes its complaint back rather than recording an
        empty one, so the field is absent from ``errors`` — see
        :meth:`~mailarc_ui.kit.validation.FieldErrors._pass`."""
        interpreter_state.set_base_url("http://gpu.example.com")
        interpreter_state.set_base_url("")

        assert BASE_URL_FIELD not in interpreter_state.errors

    async def test_cleartext_to_another_host_is_the_loudest_warning(
        self, interpreter_state
    ) -> None:
        """A stored token on a wire. The key is not re-typed when the host
        changes, so nothing else on the form would say that it travels."""
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        interpreter_state.set_base_url("http://gpu.example.com/v1")

        assert interpreter_state.host_advice.color == "red"
        assert "in the clear" in interpreter_state.host_advice.text

    async def test_a_local_host_is_not_a_leak(self, interpreter_state) -> None:
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)
        interpreter_state.set_base_url("http://localhost:11434")

        assert interpreter_state.host_advice.text == ""

    async def test_a_provider_with_no_key_has_nothing_to_leak(
        self, interpreter_state
    ) -> None:
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)
        interpreter_state.set_provider(LlmProvider.OLLAMA.value)
        interpreter_state.set_base_url("http://gpu.example.com")

        assert interpreter_state.host_advice.text == ""


class TestUnsavedChanges:
    async def test_a_typed_key_counts_as_unsaved(self, interpreter_state) -> None:
        """A key that has not been written is an unsaved change like any
        other, and the reader about to navigate away should be told."""
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)

        interpreter_state.set_api_key(SECRET)

        assert interpreter_state.settings_unsaved is True

    async def test_a_freshly_loaded_form_has_nothing_unsaved(
        self, interpreter_state
    ) -> None:
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)

        assert interpreter_state.settings_unsaved is False

    async def test_a_changed_model_counts(self, interpreter_state) -> None:
        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)

        interpreter_state.set_model("gpt-4o-mini")

        assert interpreter_state.settings_unsaved is True


class TestWhenItIsNotWiredUp:
    async def test_an_unpublished_control_is_a_sentence_not_a_key_error(self) -> None:
        """The page cannot tell a half-built application from a broken one, and
        the two have completely different fixes."""
        from appkit_commons.registry import service_registry

        from mailarc_ui.embedder.interpreter_reads import llm_control

        registry = service_registry()
        saved = registry.snapshot()
        registry.restore(type(registry)().snapshot())
        try:
            with pytest.raises(RuntimeError, match=r"app\.composition"):
                llm_control()
        finally:
            registry.restore(saved)


class TestWhenSomethingBreaks:
    """The paths a reader only ever meets on a bad day, and the one that
    decides whether a key reaches a log."""

    async def test_a_key_that_cannot_be_encrypted_takes_the_settings_with_it(
        self, interpreter_state, sessions, keyed
    ) -> None:
        """One transaction for both halves. The alternative is a stored row
        describing an OpenAI interpreter whose credential was never written,
        which fails later, elsewhere, as a 401.

        And the message is the *stripped* one: ``StatementError`` quotes its
        bind parameters, and for this one statement the bind parameter is the
        key — so a caller doing the right thing would be the thing that leaked
        it.
        """
        from appkit_commons.database.configuration import DatabaseConfig
        from appkit_commons.registry import service_registry

        registry = service_registry()
        saved = registry.snapshot()
        registry.register_as(
            DatabaseConfig,
            DatabaseConfig.model_validate({"encryption_key": "nonsense"}),
        )
        interpreter_state.set_provider(LlmProvider.OPENAI.value)
        interpreter_state.set_api_key(SECRET)
        try:
            await drive_interpreter(InterpreterSettingsState.save, interpreter_state)
        finally:
            registry.restore(saved)

        assert await llm_row(sessions) is None
        assert SECRET not in interpreter_state.error
        assert interpreter_state.saving is False

    async def test_a_load_that_cannot_read_says_so_and_stops_spinning(
        self, interpreter_state, monkeypatch
    ) -> None:
        """A form left loading forever is worse than one that says it failed:
        every control on it is disabled and nothing explains why."""
        from mailarc_ui.embedder import interpreter as module

        def broken() -> None:
            raise ConnectionError("the database went away")

        monkeypatch.setattr(module, "llm_control", broken)

        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)

        assert interpreter_state.loading is False
        assert interpreter_state.error != ""

    async def test_a_second_press_while_saving_does_nothing(
        self, interpreter_state
    ) -> None:
        """The fast race — one person double-clicking — which the timestamp
        baseline cannot see, because two writes inside one second carry the
        same value."""
        interpreter_state.saving = True

        await drive_interpreter(InterpreterSettingsState.save, interpreter_state)

        assert interpreter_state.notice == ""

    async def test_a_key_that_will_not_clear_is_a_failure_and_not_a_success(
        self, interpreter_state, monkeypatch
    ) -> None:
        from mailarc_core.database.repositories import LlmSettingsRepository

        async def broken(*args: object, **kwargs: object) -> None:
            raise ConnectionError("the database went away")

        monkeypatch.setattr(LlmSettingsRepository, "clear_api_key", broken)

        await drive_interpreter(
            InterpreterSettingsState.clear_api_key, interpreter_state
        )

        assert interpreter_state.error != ""
        assert interpreter_state.notice == ""

    async def test_a_reset_that_cannot_write_says_so(
        self, interpreter_state, monkeypatch
    ) -> None:
        from mailarc_core.database.repositories import LlmSettingsRepository

        async def broken(*args: object, **kwargs: object) -> None:
            raise ConnectionError("the database went away")

        monkeypatch.setattr(LlmSettingsRepository, "store", broken)

        await drive_interpreter(
            InterpreterSettingsState.use_configuration_file, interpreter_state
        )

        assert interpreter_state.error != ""

    async def test_a_key_from_the_configuration_is_reported_as_in_force(
        self, interpreter_state, llm_composition, llm_configured
    ) -> None:
        """Both booleans and neither a value: the browser is told *that* a key
        reaches the model, never what it is."""
        from pydantic import SecretStr

        llm_composition.effective = llm_configured.model_copy(
            update={"api_key": SecretStr("from-the-file")}
        )

        await drive_interpreter(InterpreterSettingsState.load, interpreter_state)

        assert interpreter_state.api_key_in_force is True
        assert interpreter_state.api_key_stored is False
        assert "from-the-file" not in str(interpreter_state.dict())
