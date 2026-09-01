"""The embedder form: what it shows, what it saves, and what it warns about.

Against a real SQLite file, the real
:class:`~mailarc_core.database.repositories.SemanticSettingsRepository` and the
real merge — see :mod:`embedder_form`, where the world these tests run in
lives. The write-only key and the admin gate are next door in
:mod:`test_ui_embedder_security`; this file is the form itself.

Two claims here are worth more than the rest.

**A fresh installation is untouched.** Nothing stored resolves to provider
``none`` and a dimension of 768 — §7.4's whole argument — and the page says so
rather than looking broken.

**A change that invalidates every vector says so before the save.** Three
different archives — one with vectors, one with none, one nobody could count —
earn three different sentences, and the one that matters most is the third:
a graph that did not answer must not be read as an empty one, because assuming
the cheerful case is what turns a warning into silence exactly when it counts.

No test here reaches OpenAI, Ollama or a graph.
"""

import re
from datetime import timedelta
from typing import cast
from unittest.mock import patch

import pytest
from appkit_commons.registry import service_registry
from embedder_form import (
    SECRET,
    StubSearch,
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
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from mailarc_analytics.semantic import (
    SemanticConfig,
    SemanticProvider,
    SemanticSearch,
)
from mailarc_core.database.repositories import SemanticSettingsRepository
from mailarc_ui.embedder import (
    EMBED_REMEDY,
    LOAD_FAILED,
    NO_ADVICE,
    NO_CONTROL,
    RESET,
    SAVE_FAILED,
    SAVED,
    SAVED_NOT_ADOPTED,
    SAVED_NOT_SHOWN,
    SETTINGS_MOVED,
    Advice,
    EmbedderReading,
    EmbedderSettingsState,
    identity,
    index_advice,
    semantic_control,
    vector_advice,
)
from mailarc_ui.embedder import state as state_module
from mailarc_ui.embedder.state import (
    BASE_URL_FIELD,
    DIMENSION_FIELD,
    DIMENSION_TOO_SMALL,
    NOT_AN_HTTP_URL,
)

__all__ = ["composition", "configured", "keyed", "searching", "sessions", "state"]
"""pytest collects a fixture off the importing module's namespace, so the six
are imported to be used; ``__all__`` is what stops ruff removing them again —
the same device ``test_ui_insights_rebuild.py`` uses for the same reason."""


class TestWhatAFreshInstallationSees:
    """Nothing stored resolves to the configuration file, unchanged (§7.4)."""

    async def test_the_form_opens_on_the_configured_embedder(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        assert state.provider == SemanticProvider.NONE.value
        assert state.model == ""
        assert state.dimension == 768
        assert state.api_key_stored is False
        assert state.error == ""

    async def test_nothing_is_stored_until_somebody_saves(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        assert await stored_row(sessions) is None

    async def test_an_untouched_form_warns_about_nothing(
        self, state, sessions, searching
    ) -> None:
        """The advice is about a *pending* change; an unedited form has none."""
        await load_form(state)

        assert state.vector_advice == NO_ADVICE
        assert state.index_advice == NO_ADVICE

    async def test_the_key_status_says_there_is_none(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        assert state.key_status == "No key is stored."
        assert state.key_pending is False


class TestSavingChangesWhatTheArchiveUses:
    async def test_a_save_is_stored_and_adopted(
        self, state, sessions, searching, composition
    ) -> None:
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)
        await state.set_model("nomic-embed-text")
        await state.set_base_url("http://localhost:11434")

        await save_form(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert (stored.provider, stored.model) == ("ollama", "nomic-embed-text")
        # The whole point: what the archive would now build an embedder from.
        assert composition.effective.provider is SemanticProvider.OLLAMA
        assert composition.effective.model == "nomic-embed-text"
        assert state.notice == SAVED
        assert state.error == ""

    async def test_an_unset_field_falls_through_to_the_file(
        self, state, sessions, searching, composition
    ) -> None:
        """The form saves what is in force, so the file's dimension survives.

        The trap this closes: a form pre-filled from the *stored* row would
        show an empty dimension on an installation configured in
        ``config.yaml``, and the first save would write that emptiness over a
        working configuration.
        """
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)

        await save_form(state)

        assert composition.effective.dimension == 768
        assert state.dimension == 768

    async def test_a_zero_dimension_is_refused_rather_than_stored(
        self, state, sessions, searching
    ) -> None:
        """An emptied box is a legal thing to hold and an illegal thing to save."""
        await load_form(state)
        await state.set_dimension("")

        assert state.dimension == 0
        assert state.can_save is False
        assert state.index_advice.color == "red"

    async def test_a_half_typed_number_is_ignored_rather_than_crashing(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        await state.set_dimension("12e")

        assert state.dimension == 768

    async def test_an_unknown_provider_changes_nothing(
        self, state, sessions, searching
    ) -> None:
        """The argument arrives over the socket, so it is not trusted."""
        await load_form(state)

        await state.set_provider("anthropic")

        assert state.provider == SemanticProvider.NONE.value

    async def test_a_save_the_application_cannot_adopt_says_so(
        self, state, sessions, searching, composition
    ) -> None:
        """Two outcomes, because "Saved" over an unchanged embedder is a lie."""
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)
        composition.breaks = True

        await save_form(state)

        assert (await stored_row(sessions)) is not None
        assert state.notice == SAVED_NOT_ADOPTED

    async def test_a_database_that_refuses_the_write_keeps_the_form(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)

        with patch.object(
            SemanticSettingsRepository, "store", side_effect=ConnectionError("gone")
        ):
            await save_form(state)

        assert state.error == SAVE_FAILED
        assert state.saving is False
        assert "gone" not in state.error


class TestUsingTheConfigurationFileAgain:
    async def test_it_forgets_every_stored_value_and_the_key(
        self, state, sessions, searching, composition
    ) -> None:
        """Without this a single save would be one-way."""
        await load_form(state)
        await state.set_provider(SemanticProvider.OPENAI.value)
        await state.set_model("text-embedding-3-small")
        await state.set_api_key(SECRET)
        await save_form(state)

        await reset_form(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert (stored.provider, stored.model, stored.api_key) == (None, None, None)
        assert composition.effective.provider is SemanticProvider.NONE
        assert state.provider == SemanticProvider.NONE.value
        assert state.api_key_stored is False
        assert state.notice == RESET


class TestTheDimensionAndModelWarnings:
    """§7.4's trap: a mismatch does not fail, it disappears."""

    def test_a_change_with_vectors_behind_it_names_the_count_and_the_cost(
        self,
    ) -> None:
        reading = EmbedderReading(
            provider="ollama",
            model="nomic-embed-text",
            dimension=768,
            embedded=12431,
            coverage_known=True,
        )

        said = vector_advice(reading, provider="openai", model="text-embedding-3-small")

        assert "12431" in said.text
        assert "nomic-embed-text" in said.text
        assert "text-embedding-3-small" in said.text
        assert EMBED_REMEDY in said.text
        assert said.color == "yellow"

    def test_a_change_on_an_empty_archive_guides_instead_of_warning(self) -> None:
        reading = EmbedderReading(provider="none", dimension=768, coverage_known=True)

        said = vector_advice(reading, provider="ollama", model="nomic-embed-text")

        assert said.color == "blue"
        assert "nothing is invalidated" in said.text
        assert EMBED_REMEDY in said.text

    def test_an_uncountable_archive_is_warned_as_though_it_were_full(self) -> None:
        """The case that decides whether the warning is worth having.

        A graph that did not answer must not be read as an empty one: assuming
        the cheerful case is what turns a warning into silence exactly when it
        matters.
        """
        reading = EmbedderReading(
            provider="ollama", model="nomic-embed-text", coverage_known=False
        )

        said = vector_advice(reading, provider="openai", model="")

        assert said.color == "yellow"
        assert "could not be read" in said.text
        assert EMBED_REMEDY in said.text

    def test_no_change_earns_no_sentence(self) -> None:
        reading = EmbedderReading(provider="ollama", model="x", coverage_known=True)

        assert vector_advice(reading, provider="ollama", model="x") is NO_ADVICE
        assert index_advice(reading, dimension=reading.dimension) is NO_ADVICE

    def test_a_changed_dimension_says_the_index_is_the_problem(self) -> None:
        reading = EmbedderReading(provider="openai", model="m", dimension=768)

        said = index_advice(reading, dimension=1536)

        assert "768" in said.text
        assert "1536" in said.text
        assert "silently not indexed" in said.text
        assert said.color == "red"

    def test_an_empty_model_is_named_rather_than_left_blank(self) -> None:
        assert identity("ollama", "") == "ollama / the provider's own default model"
        assert identity("none", "") == "no embedder"
        assert identity("", "") == "no embedder"

    async def test_the_form_warns_before_the_save_and_not_after(
        self, state, sessions, searching, composition
    ) -> None:
        """The whole requirement in one test: said first, gone once done."""
        searching.embedded = 40
        await load_form(state)

        await state.set_provider(SemanticProvider.OLLAMA.value)
        await state.set_model("nomic-embed-text")

        assert "40 message(s) are already embedded" in state.vector_advice.text
        await save_form(state)
        assert state.vector_advice == NO_ADVICE

    async def test_a_graph_that_does_not_answer_still_opens_the_form(
        self, state, sessions, composition
    ) -> None:
        """Configuring an embedder is what you do *before* the graph helps."""
        stub = StubSearch(error=ConnectionError("graph is down"))
        service_registry().register_as(SemanticSearch, cast(SemanticSearch, stub))

        await load_form(state)

        assert state.error == ""
        assert state.in_force.coverage_known is False

    async def test_an_unpublished_search_is_not_a_broken_form(
        self, state, sessions, composition
    ) -> None:
        await load_form(state)

        assert state.in_force.coverage_known is False


class TestTheWaysAWriteCanGoWrong:
    """Each failure leaves the form usable and says which half went wrong."""

    async def test_a_second_save_while_one_is_running_does_nothing(
        self, state, sessions, searching
    ) -> None:
        """Two clicks on a slow button must not be two transactions."""
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)
        state.saving = True

        await save_form(state)

        assert await stored_row(sessions) is None

    async def test_a_failed_clear_keeps_the_key_and_says_so(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        await state.set_api_key(SECRET)
        await save_form(state)

        with patch.object(
            SemanticSettingsRepository,
            "clear_api_key",
            side_effect=ConnectionError("gone"),
        ):
            await clear_key(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert stored.api_key == SECRET
        assert state.error == SAVE_FAILED
        assert state.saving is False

    async def test_a_failed_reset_leaves_the_stored_settings_standing(
        self, state, sessions, searching, composition
    ) -> None:
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)
        await save_form(state)

        with patch.object(
            SemanticSettingsRepository, "store", side_effect=ConnectionError("gone")
        ):
            await reset_form(state)

        assert composition.effective.provider is SemanticProvider.OLLAMA
        assert state.error == SAVE_FAILED

    async def test_a_save_that_cannot_be_read_back_says_which_half_worked(
        self, state, sessions, searching
    ) -> None:
        """The write landed; the re-read did not. The form must not pretend.

        Without this the page would show the values it had before the save,
        under a green "Saved" — which is the one screen that would make
        somebody save a second time.
        """
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)

        with patch.object(
            SemanticSettingsRepository,
            "api_key_is_set",
            side_effect=ConnectionError("the database went away"),
        ):
            await save_form(state)

        assert await stored_row(sessions) is not None
        assert state.notice == ""
        assert state.error == SAVED_NOT_SHOWN
        assert "went away" not in state.error, (
            "a driver message can carry a path out of this installation, and "
            "this string is rendered into a browser — the same policy "
            "SAVE_FAILED states one method away"
        )
        assert state.saving is False


class TestFindingTheCompositionRoot:
    def test_the_published_control_is_the_one_the_state_uses(self, composition) -> None:
        assert semantic_control().current() is composition.effective

    def test_an_unpublished_control_is_a_sentence_not_a_key_error(self) -> None:
        services = service_registry()
        saved = services.snapshot()
        try:
            services.restore({})
            with pytest.raises(RuntimeError, match=re.escape("app.composition")):
                semantic_control()
        finally:
            services.restore(saved)

    async def test_a_page_without_one_says_which_wiring_is_missing(
        self, sessions
    ) -> None:
        services = service_registry()
        saved = services.snapshot()
        try:
            orphan = EmbedderSettingsState()

            await load_form(orphan)

            assert orphan.error == NO_CONTROL
            assert orphan.loading is False
        finally:
            services.restore(saved)


class TestWhatTheFormOffers:
    def test_the_openai_option_says_what_choosing_it_means(self) -> None:
        """A list of three words cannot tell somebody their mail is uploaded."""
        from mailarc_ui.embedder import PROVIDER_OPTIONS

        openai = next(
            one for one in PROVIDER_OPTIONS if one["value"] == SemanticProvider.OPENAI
        )
        assert "third party" in openai["label"]
        assert {one["value"] for one in PROVIDER_OPTIONS} == {
            one.value for one in SemanticProvider
        }

    async def test_openai_without_a_stored_key_is_flagged(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        await state.set_provider(SemanticProvider.OPENAI.value)

        assert state.key_matters is True
        assert state.key_missing is True

    async def test_ollama_does_not_ask_for_a_key(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        await state.set_provider(SemanticProvider.OLLAMA.value)

        assert state.key_matters is False
        assert state.key_missing is False

    async def test_every_control_is_dead_before_the_first_read_lands(self) -> None:
        """A live control over an unloaded form is a promise."""
        fresh = EmbedderSettingsState()

        assert fresh.blocked is True
        assert fresh.can_save is False
        assert fresh.can_clear_key is False
        assert fresh.vector_advice == NO_ADVICE

    async def test_the_advice_is_a_value_object_the_component_can_read(self) -> None:
        """Reflex resolves ``row.field`` on a ``BaseModel``; a dict it would not."""
        assert isinstance(NO_ADVICE, Advice)
        assert NO_ADVICE.text == ""


class TestAKeyThatComesFromTheConfigurationFile:
    """The installation ``docs/user/semantic-search.md`` documents, and no fixture had.

    ``app.semantic.api_key`` in ``config.yaml`` (or ``app_semantic_api_key`` in
    the environment) with provider ``openai``: embedding works, and the form
    used to answer from the *stored row* alone — a grey badge saying "No key is
    stored." over a yellow alert promising a 401 that could not happen. The
    correct value was already in hand: ``_read`` fetches the merged config on
    the line above the one it built the reading from.

    Why it mattered rather than merely read wrongly: the obvious reaction is to
    paste the key into the form, which puts a second copy of the secret in the
    database — and the stored row *wins* the merge, so the next rotation in
    ``config.yaml`` is silently ignored.
    """

    @pytest.fixture
    def configured(self) -> SemanticConfig:
        return SemanticConfig(
            provider=SemanticProvider.OPENAI,
            model="text-embedding-3-small",
            dimension=768,
            api_key=SecretStr("sk-from-the-configuration-file"),
        )

    async def test_the_form_does_not_claim_there_is_no_key(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        assert state.key_missing is False
        assert "No key is stored" not in state.key_status

    async def test_it_says_where_the_key_comes_from(
        self, state, sessions, searching
    ) -> None:
        """Three cases, three sentences: stored here, from the file, nowhere.

        The middle one is also what makes the Clear button's consequence
        readable — and the page already promised it in ``KEY_CLEARED``, which
        correctly says the file's key takes over. Saying both "a configuration
        key exists" and "no key exists" on one page is the state this fixes.
        """
        await load_form(state)

        assert "configuration file" in state.key_status

    async def test_there_is_still_nothing_here_to_clear(
        self, state, sessions, searching
    ) -> None:
        """``api_key_stored`` keeps its old meaning, which is the clearable one.

        Clear writes ``NULL`` to a column; a key in ``config.yaml`` is not
        reachable from this page and offering a button that cannot do what it
        says would be worse than the wrong badge.
        """
        await load_form(state)

        assert state.api_key_stored is False
        assert state.can_clear_key is False

    async def test_the_key_itself_never_reaches_the_browser(
        self, state, sessions, searching
    ) -> None:
        """The write-only rule holds for the file's key too.

        The reading carries ``api_key is not None`` and never the value —
        asking whether something is ``None`` reveals nothing about it.
        """
        await load_form(state)

        assert "sk-from-the-configuration-file" not in everything_the_browser_would_see(
            state
        )


class TestTheFormReadsTheLiveVectorIndex:
    """The dimension warning compares against the graph, not the configuration."""

    async def test_the_index_length_reaches_the_reading(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        assert state.in_force.index_known is True
        assert state.in_force.index_dimension == searching.index

    async def test_a_graph_that_cannot_be_read_leaves_it_unknown(
        self, state, sessions, searching
    ) -> None:
        """The same policy the vector count gets, and for the same reason: a
        settings page has to work before the graph does."""
        searching.error = RuntimeError("the graph is not running")

        await load_form(state)

        assert state.in_force.index_known is False
        assert state.error == ""

    async def test_a_standing_mismatch_is_flagged_on_load(
        self, state, sessions, searching
    ) -> None:
        """Nothing typed, and the page still says the archive is broken.

        This is the case the old comparison could not reach at all: the
        configured length equals the typed length, so it returned NO_ADVICE
        while every vector written was being dropped by the index.
        """
        searching.index = 1536

        await load_form(state)

        assert state.index_advice.color == "red"
        assert "1536" in state.index_advice.text


class TestWhatMakesTheSettingsValid:
    """The two rules, and the button that reads their verdict.

    Both replace a silence. The dimension was refused by ``can_save`` alone,
    so a zero left the button dead with nothing on the page saying why; the
    base URL was discarded on the keystroke, so the box could not be typed
    into at all.
    """

    async def test_a_dimension_of_zero_says_why_the_button_is_dead(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        await state.set_dimension("")

        assert state.errors[DIMENSION_FIELD] == DIMENSION_TOO_SMALL
        assert state.can_save is False

    async def test_a_real_length_clears_it(self, state, sessions, searching) -> None:
        await load_form(state)
        await state.set_dimension("")

        await state.set_dimension(768)

        assert state.errors == {}
        assert state.can_save is True

    async def test_a_dimension_of_zero_is_never_written(
        self, state, sessions, searching, composition
    ) -> None:
        """``can_save`` is a UI gate; this is the boundary."""
        await load_form(state)
        await state.set_dimension("")

        await save_form(state)

        assert await stored_row(sessions) is None


class TestChangingTheHost:
    async def test_a_new_base_url_is_warned_about(
        self, state, sessions, searching
    ) -> None:
        """The one change that was saved in silence."""
        await load_form(state)

        await state.set_base_url("http://gpu.internal:11434")

        assert state.host_advice.text != ""

    async def test_something_that_is_not_a_url_says_so_on_the_field(
        self, state, sessions, searching
    ) -> None:
        """Held and marked, rather than swallowed.

        The box used to discard anything that was not already an absolute URL,
        which meant it could not be typed into: ``h`` is not one either. What
        the value must never do is reach a write, and that is the test below —
        this one is only that a person can see why.
        """
        await load_form(state)

        await state.set_base_url("gpu.internal:11434")

        assert state.base_url == "gpu.internal:11434"
        assert state.errors[BASE_URL_FIELD] == NOT_AN_HTTP_URL
        assert state.can_save is False

    async def test_something_that_is_not_a_url_is_never_written(
        self, state, sessions, searching, composition
    ) -> None:
        """The guarantee itself, at the boundary it is about.

        It arrives over a socket, where an event's arguments are whatever the
        caller sent, and this one decides which host receives the archive's
        stored bearer token on every embedding call. So the assertion is not
        that a control refused it — a caller need not use the control — but
        that pressing Save with it in the form stores nothing at all.
        """
        await load_form(state)
        await state.set_base_url("gpu.internal:11434")

        await save_form(state)

        assert await stored_row(sessions) is None

    async def test_emptying_it_still_means_the_providers_own(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        await state.set_base_url("http://gpu.internal:11434")

        await state.set_base_url("")

        assert state.base_url == ""
        assert state.errors == {}


class TestWhatTheFormSaysWhenItCannotRead:
    """``SAVE_FAILED`` exists because a driver message can carry a path.

    The load path contradicted that policy one method away, on the same page:
    it rendered ``str(error)`` into the browser, and an un-migrated database —
    a case ``semantic_settings_lifespan`` names as real — produces a
    ``StatementError`` carrying the statement, the bind parameters and, for a
    SQLite archive opened by path, the store's location.
    """

    async def test_a_driver_failure_is_reported_without_quoting_the_driver(
        self, state, sessions, searching, monkeypatch
    ) -> None:
        async def refusing(self: EmbedderSettingsState) -> EmbedderReading:
            raise OperationalError(
                "SELECT semantic_settings.api_key IS NOT NULL",
                {},
                RuntimeError("unable to open database file /Users/someone/.state"),
            )

        monkeypatch.setattr(EmbedderSettingsState, "_read", refusing)

        await load_form(state)

        assert state.error == LOAD_FAILED
        assert "/Users/someone" not in state.error

    async def test_the_half_wired_build_still_says_so(
        self, state, sessions, searching, monkeypatch
    ) -> None:
        """The one exception worth showing verbatim, because it is ours.

        A half-wired application and a broken one look identical from a form
        and have completely different fixes, which is why ``NO_CONTROL`` is a
        sentence rather than a ``KeyError``. Losing it behind a generic
        "could not be read" would undo that.
        """

        async def refusing(self: EmbedderSettingsState) -> EmbedderReading:
            raise RuntimeError(NO_CONTROL)

        monkeypatch.setattr(EmbedderSettingsState, "_read", refusing)

        await load_form(state)

        assert state.error == NO_CONTROL


class TestATypedKeyDoesNotOutliveTheFormItWasTypedInto:
    async def test_reloading_forgets_a_key_that_was_typed_and_abandoned(
        self, state, sessions, searching
    ) -> None:
        """Reload is a request for the stored truth, and half a secret is not part of it.

        The password box is uncontrolled — ``default_value=""``, no ``value``
        prop — so nothing in the DOM contradicted the impression of a clean
        form either, and the abandoned key was then written by the next
        unrelated save.
        """
        await load_form(state)
        await state.set_api_key("sk-typed-then-thought-better-of")

        await load_form(state)

        assert state.key_pending is False

    async def test_a_save_after_that_reload_stores_nothing(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        await state.set_api_key("sk-typed-then-thought-better-of")
        await load_form(state)

        await state.set_model("nomic-embed-text")
        await save_form(state)

        row = await stored_row(sessions)
        assert row is not None
        assert row.api_key is None


class TestTheWritingHandlersRefuseToRunTwiceAtOnce:
    """``save`` guards on :attr:`saving`; its two neighbours did not.

    Both are background handlers, so two clicks in quick succession start two
    tasks — each completing a write and each calling ``_adopt``, which reloads
    the composition root and can ``aclose()`` an embedder the other task is
    concurrently publishing.
    """

    async def test_clearing_the_key_twice_at_once_writes_once(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        state.saving = True

        await clear_key(state)

        assert state.saving is True, "the first task still owns the write"

    async def test_forgetting_the_settings_twice_at_once_writes_once(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)
        state.saving = True

        await reset_form(state)

        assert await stored_row(sessions) is None


class TestTwoAdministratorsAtTwoScreens:
    """One row for the whole archive, and every save overwrites all of it.

    What makes a lost update here worse than usual is *which* settings these
    are: putting a dimension back silently undoes the change the vector index
    was migrated for, and the archive then embeds at a length the index does
    not carry — accepted, stored, never indexed, reported nowhere. The stale
    editor's form compares against the stale editor's baseline, so it does not
    even warn.
    """

    async def test_a_save_from_a_stale_form_is_refused_rather_than_applied(
        self, state, sessions, searching, composition, monkeypatch
    ) -> None:
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)
        await state.set_dimension(768)
        await save_form(state)
        # Somebody else, at another screen, a minute later.
        async with sessions() as session:
            await SemanticSettingsRepository().store(
                session,
                provider=SemanticProvider.OPENAI.value,
                model="text-embedding-3-large",
                dimension=1536,
                base_url="",
            )
            row = await SemanticSettingsRepository().load(session)
            assert row is not None
            row.updated = row.updated + timedelta(minutes=1)

        await state.set_model("nomic-embed-text")
        await save_form(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert stored.provider == SemanticProvider.OPENAI.value
        assert stored.dimension == 1536
        assert state.error == SETTINGS_MOVED

    async def test_the_refused_form_is_shown_what_is_actually_stored_now(
        self, state, sessions, searching, composition
    ) -> None:
        """A refusal that left the stale values on screen would invite a retry.

        The second press would carry the baseline the first one just re-read,
        so it would succeed — and quietly do the damage the refusal prevented.
        """
        await load_form(state)
        await save_form(state)
        async with sessions() as session:
            await SemanticSettingsRepository().store(
                session,
                provider=SemanticProvider.OPENAI.value,
                model="text-embedding-3-large",
                dimension=1536,
                base_url="",
            )
            row = await SemanticSettingsRepository().load(session)
            assert row is not None
            row.updated = row.updated + timedelta(minutes=1)

        await save_form(state)

        assert state.provider == SemanticProvider.OPENAI.value
        assert state.model == "text-embedding-3-large"

    async def test_an_ordinary_second_save_from_the_same_form_still_works(
        self, state, sessions, searching, composition
    ) -> None:
        """The baseline moves with each save, or the form would work once."""
        await load_form(state)
        await state.set_provider(SemanticProvider.OLLAMA.value)
        await save_form(state)

        await state.set_model("mxbai-embed-large")
        await save_form(state)

        stored = await stored_row(sessions)
        assert stored is not None
        assert stored.model == "mxbai-embed-large"
        assert state.error == ""


class TestTheDimensionFollowsTheProvider:
    """Choosing a provider offers the length that provider actually produces.

    The number is not guessable and getting it wrong fails in the worst way the
    project has: a vector of the wrong length is accepted, stored and silently
    never indexed, so search simply finds nothing. Ollama's ``nomic-embed-text``
    is 768 and cannot be asked for fewer; OpenAI's ``text-embedding-3-small`` is
    1536 and ``-3-large`` 3072, and both can be asked for fewer because they are
    trained so a prefix is itself usable. So the form fills the field in rather
    than leaving somebody to look it up.

    It fills it in only when a human changes the choice. Loading a stored row
    must never rewrite a length somebody set on purpose — for OpenAI a shorter
    vector is a legitimate choice, and silently restoring 1536 on every page
    load would undo it.
    """

    async def _opened(self) -> EmbedderSettingsState:
        state = EmbedderSettingsState()
        state.loading = False
        return state

    async def test_choosing_openai_offers_1536(self) -> None:
        state = await self._opened()
        state.dimension = 768

        await EmbedderSettingsState.set_provider.fn(state, "openai")

        assert state.dimension == 1536

    async def test_choosing_ollama_offers_768(self) -> None:
        state = await self._opened()
        state.dimension = 1536

        await EmbedderSettingsState.set_provider.fn(state, "ollama")

        assert state.dimension == 768

    async def test_naming_the_large_model_offers_3072(self) -> None:
        """The length is the model's, not the vendor's — one account ships both."""
        state = await self._opened()
        await EmbedderSettingsState.set_provider.fn(state, "openai")

        await EmbedderSettingsState.set_model.fn(state, "text-embedding-3-large")

        assert state.dimension == 3072

    async def test_an_unknown_model_leaves_the_length_alone(self) -> None:
        """Nobody here knows its length, so inventing one would be a guess."""
        state = await self._opened()
        await EmbedderSettingsState.set_provider.fn(state, "openai")
        state.dimension = 1024

        await EmbedderSettingsState.set_model.fn(state, "some-local-finetune")

        assert state.dimension == 1024

    async def test_a_length_set_by_hand_survives_until_the_provider_changes(
        self,
    ) -> None:
        """768 against a 1536-native OpenAI model is a real choice, not a slip."""
        state = await self._opened()
        await EmbedderSettingsState.set_provider.fn(state, "openai")

        await EmbedderSettingsState.set_dimension.fn(state, 768)
        await EmbedderSettingsState.set_model.fn(state, "text-embedding-3-small")

        assert state.dimension == 768, (
            "naming the model that is already chosen re-offered its native "
            "length and overwrote a deliberate 768"
        )


class TestRebuildingTheIndex:
    """The control that makes the length a setting rather than a display field."""

    async def _opened(self) -> EmbedderSettingsState:
        state = EmbedderSettingsState()
        state.loading = False
        return state

    async def test_it_reports_what_it_forgot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resize discards every vector; the notice has to say how many."""
        state = await self._opened()
        called: list[int] = []
        monkeypatch.setattr(
            state_module, "semantic_control", lambda: _control(called, cleared=7)
        )
        monkeypatch.setattr(EmbedderSettingsState, "_adopt", _record_notice)

        await EmbedderSettingsState.rebuild_index.fn(state)

        assert called == [1]
        assert "7" in state.notice
        assert state.reindexing is False

    async def test_a_failure_leaves_the_button_usable_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rebuild that raised must not leave the form stuck mid-rebuild."""
        state = await self._opened()

        def exploding() -> object:
            class _C:
                async def reindex(self) -> int:
                    raise RuntimeError("the graph is down")

            return _C()

        monkeypatch.setattr(state_module, "semantic_control", exploding)
        monkeypatch.setattr(EmbedderSettingsState, "_failed", _record_error)

        await EmbedderSettingsState.rebuild_index.fn(state)

        assert state.reindexing is False, "the form is stuck showing a rebuild"
        assert state.error


class TestThePageOpensWhereEveryOtherPageOpens:
    """An empty alert block is not weightless, so it must not be rendered.

    Measured on the running application before this was fixed: the first card
    on ``/admin/embedder`` sat 54px below the top of the main area while the
    one on ``/insights`` and ``/admin/status`` sat at 24px. The thirty pixels
    were an empty ``mn.stack`` holding two empty alerts — its own ``gap="xs"``
    between two zero-height children, and the panel's ``gap="lg"`` underneath
    the block — and a page opens with nothing to report every time.
    """

    async def test_a_page_with_nothing_to_report_has_no_message_block(
        self, state, sessions, searching
    ) -> None:
        await load_form(state)

        assert state.has_message is False

    async def test_an_error_brings_the_block_back(self, state) -> None:
        state.error = "That did not work"

        assert state.has_message is True

    async def test_a_notice_brings_the_block_back(self, state) -> None:
        state.notice = "Saved."

        assert state.has_message is True


def _control(seen: list[int], cleared: int = 0) -> object:
    """A control whose reindex records that it was called."""

    class _C:
        async def reindex(self) -> int:
            seen.append(1)
            return cleared

    return _C()


async def _record_notice(self: EmbedderSettingsState, notice: str) -> None:
    self.notice = notice


async def _record_error(self: EmbedderSettingsState, message: str) -> None:
    self.error = message
