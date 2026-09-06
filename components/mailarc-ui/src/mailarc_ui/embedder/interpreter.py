"""The second half of the models page: the model that reads a question.

:mod:`mailarc_ui.embedder.state`'s sibling, and deliberately a separate state
class rather than that one taught a second set of fields. The two forms write
different tables, validate different things and cost different amounts to get
wrong — an embedder change strands every stored vector, while an interpreter
change costs the next question — so one class holding both would spend most of
its length saying which half a var belonged to.

What is *not* duplicated is the machinery underneath: the write-only key rule,
the concurrency baseline and the "let the file answer again" reset are all
:class:`~mailarc_core.database.repositories.LlmSettingsRepository`'s, which is
the embedder repository's twin down to the guarded flush.

Three things this half does not have, each because nothing here is stored:

- **No dimension**, so no index to disagree with a setting.
- **No warning before a save.** Changing an embedder is warned about with the
  number of messages it strands; changing this strands nothing.
- **No rebuild.** There is nothing to recompute.
"""

import logging
from datetime import datetime

import reflex as rx

from mailarc_analytics.nlq import LlmConfig, LlmProvider
from mailarc_core.database.repositories import (
    ApiKeyNotStored,
    SettingsChangedElsewhere,
)
from mailarc_ui.embedder.interpreter_reads import (
    clear_llm_api_key,
    llm_control,
    reset_llm_settings,
    stored_llm_baseline,
    write_llm_settings,
)
from mailarc_ui.embedder.model import (
    KEY_CLEARED,
    KEY_NOT_STORED,
    NO_ADVICE,
    SAVE_FAILED,
    SAVED,
    Advice,
    is_cleartext_to_elsewhere,
)
from mailarc_ui.embedder.reads import is_absolute_http
from mailarc_ui.kit.validation import FieldErrors

logger = logging.getLogger(__name__)

BASE_URL_FIELD = "llm_base_url"
"""Which field a bad base URL is reported against.

Its own name rather than the embedder form's, because both forms are on one
page and one shared key would light the wrong field's error.
"""

BAD_BASE_URL = "Enter an absolute http:// or https:// URL, or leave it empty."

KEYED_PROVIDERS = (LlmProvider.OPENAI.value, LlmProvider.AZURE_OPENAI.value)
"""The providers that answer 401 without a key.

Ollama ignores one entirely, so warning about a missing key there would be
warning about a setting that changes nothing.
"""

PROVIDER_OPTIONS = [
    {"value": LlmProvider.NONE.value, "label": "None — natural search is off"},
    {"value": LlmProvider.OLLAMA.value, "label": "Ollama (local, no account)"},
    {"value": LlmProvider.OPENAI.value, "label": "OpenAI"},
    {"value": LlmProvider.AZURE_OPENAI.value, "label": "Azure OpenAI"},
]
"""What the picker offers, each labelled with what choosing it costs.

``none`` says what it turns off rather than reading as an empty value, and the
local one says it needs no account — which is the whole reason it is the one a
desktop installation can turn on.
"""

SETTINGS_MOVED = (
    "Somebody else changed the interpreter settings while this form was open, "
    "so nothing was written — saving would have put their change back without "
    "either of you being told. What is below is what is stored now; make your "
    "change again over it."
)
"""The refusal, and its own sentence rather than the embedder's reused.

The two forms are on one page and save independently, so a message naming the
wrong half would send a reader to re-check settings nobody had touched.
"""


class InterpreterSettingsState(FieldErrors, rx.State):
    """The interpreter form: what is in force, and what to change it to.

    Three settable values and a key that is not one of them —
    :class:`~mailarc_ui.embedder.state.EmbedderSettingsState`'s shape with the
    length and the rebuild taken out.
    """

    provider: str = LlmProvider.NONE.value
    model: str = ""
    base_url: str = ""

    api_key_stored: bool = False
    """Whether a key is stored *here*, which is the only one Clear can forget."""

    api_key_in_force: bool = False
    """Whether a key reaches the model at all, from wherever it comes.

    Both booleans and neither a value: a browser is told *that* a key exists
    and never what it is.
    """

    in_force_provider: str = LlmProvider.NONE.value
    in_force_model: str = ""
    """The interpreter as the last load found it.

    Not a copy of the fields above — those move as somebody types and these do
    not — so that :attr:`settings_unsaved` can tell the two apart.
    """

    loading: bool = True
    saving: bool = False
    error: str = ""
    notice: str = ""

    _typed_key: str = ""
    """The API key as it is being typed, and the only place it exists.

    Backend-only — Reflex ships no var whose name starts with an underscore —
    for the reason ``EmbedderSettingsState._typed_key`` is: a secret has no
    business being sent back to the browser it came from.
    """

    _baseline: datetime | None = None
    """The stored row's timestamp as this form last read it, or ``None``.

    Backend-only, because nothing on screen is decided by it: it exists so a
    save can say "if the row is still the one I read".
    """

    @rx.var
    def blocked(self) -> bool:
        """Whether the fields are read-only right now."""
        return self.loading or self.saving

    @rx.var
    def key_matters(self) -> bool:
        """Whether the chosen provider does anything with a key at all."""
        return self.provider in KEYED_PROVIDERS

    @rx.var
    def key_status(self) -> str:
        """What the badge under the key box says."""
        if self.api_key_stored:
            return "A key is stored"
        if self.api_key_in_force:
            return "A key comes from the configuration"
        return "No key stored"

    @rx.var
    def key_pending(self) -> bool:
        """Whether a key has been typed and not yet written."""
        return self._typed_key.strip() != ""

    @rx.var
    def key_missing(self) -> bool:
        """Whether this provider needs a key and none will reach it."""
        return (
            self.key_matters
            and not self.api_key_in_force
            and not self.api_key_stored
            and not self.key_pending
        )

    @rx.var
    def can_clear_key(self) -> bool:
        """Only a key stored *here* can be forgotten from here.

        One that comes from the configuration file is not this page's to
        remove, and a button that looked like it could would be a button that
        silently did nothing.
        """
        return self.api_key_stored and not self.blocked

    @rx.var
    def can_save(self) -> bool:
        return not self.blocked

    @rx.var
    def interpreter_configured(self) -> bool:
        return self.in_force_provider != LlmProvider.NONE.value

    @rx.var
    def settings_unsaved(self) -> bool:
        """Whether the form says something the archive is not running with.

        The key is deliberately in it: a typed key that has not been written
        is an unsaved change like any other, and the reader who is about to
        navigate away should be told.
        """
        return (
            self.provider != self.in_force_provider
            or self.model.strip() != self.in_force_model
            or self.key_pending
        )

    @rx.var
    def host_advice(self) -> Advice:
        """Whether this base URL would put the stored key on a wire in the clear.

        Deliberately **not** :func:`~mailarc_ui.embedder.model.host_advice`.
        That one's first and longer half is about vector spaces — two hosts
        serving a model of the same name answer in different spaces, and the
        stored vectors cannot tell — and none of it applies here, because
        nothing this model produces is stored. Reusing it would warn about a
        cost this form does not have.

        What does carry over is the credential, and it is shared at the level
        where it really is one question:
        :func:`~mailarc_ui.embedder.model.is_cleartext_to_elsewhere`.
        ``OpenAIChat._headers`` and ``AzureOpenAIChat._headers`` attach the
        stored key to every call at whatever URL is configured, and the key is
        not re-typed when the host changes — so nothing else on this form would
        say that the secret now travels somewhere new.
        """
        url = self.base_url.strip()
        if not url or self.provider not in KEYED_PROVIDERS:
            return NO_ADVICE
        if is_cleartext_to_elsewhere(url):
            return Advice(
                text=(
                    f"This sends the stored API key to `{url}` on every "
                    f"question, and that address is not encrypted — the token "
                    f"would cross the network in the clear."
                ),
                color="red",
            )
        return Advice(
            text=(
                f"This sends the stored API key to `{url}` on every question. "
                f"The key is not re-typed for a change of host, so nothing "
                f"else here says it travels."
            )
        )

    @rx.var
    def has_message(self) -> bool:
        return bool(self.error or self.notice)

    @rx.event(background=True)
    async def load(self) -> None:
        """Read what is in force and what is stored, on every page open.

        Two reads that fail together, because the form is one thing: the merged
        configuration says what the archive is running with, and the stored row
        says whether the key and the timestamp are this page's to work with.
        """
        async with self:
            self.loading = True
            self.error, self.notice = "", ""
        try:
            current = llm_control().current()
            stored, baseline = await stored_llm_baseline()
        except Exception:
            logger.exception("Could not read the interpreter settings")
            async with self:
                self.loading = False
                self.error = SAVE_FAILED
            return
        async with self:
            self._apply(current, stored, baseline)
            self.loading = False

    def _apply(
        self, current: LlmConfig, stored: bool, baseline: datetime | None
    ) -> None:
        """One reading into the form, all of it at once."""
        self.provider = current.provider.value
        self.model = current.model
        self.base_url = current.base_url
        self.in_force_provider = current.provider.value
        self.in_force_model = current.model
        self.api_key_stored = stored
        self.api_key_in_force = current.api_key is not None
        self._baseline = baseline
        self._typed_key = ""
        self._clear_errors()

    @rx.event
    def set_provider(self, value: str) -> None:
        """Only a provider this build knows; anything else is off.

        The narrowing every picker on this project keeps — the value arrives
        over a socket, and one nobody offered would be stored and then refused
        at the first question.
        """
        self.provider = (
            value
            if value in {one["value"] for one in PROVIDER_OPTIONS}
            else self.provider
        )

    @rx.event
    def set_model(self, value: str) -> None:
        self.model = value

    @rx.event
    def set_base_url(self, value: str) -> None:
        self.base_url = value
        self._validate_base_url()

    @rx.event
    def set_api_key(self, value: str) -> None:
        """Hold a typed key in the backend and nowhere else."""
        self._typed_key = value

    def _validate_base_url(self) -> bool:
        """Empty is fine — it means the provider's own endpoint."""
        text = self.base_url.strip()
        wrong = bool(text) and not is_absolute_http(text)
        return self._check(BASE_URL_FIELD, BAD_BASE_URL if wrong else "")

    @rx.event(background=True)
    async def save(self) -> None:
        """Write the settings, then make the running application adopt them.

        One transaction for the settings and the key, so the one way the key
        write can fail takes the settings with it rather than leaving a row
        describing a provider whose credential was never stored.

        The adoption is a second step and may fail on its own: the row is
        written either way, and the two outcomes have to read differently
        because "Saved" over an application still using the previous model
        would send somebody looking for the fault in the model.
        """
        async with self:
            if self.saving:
                return
            # Where the base-URL guarantee lives. The setter marks the field so
            # somebody can see what is wrong while they type; this is what
            # keeps a value that is not an absolute http(s) URL from reaching
            # the write, and it runs whatever the button was doing.
            if not self._validate_base_url():
                return
            self.saving = True
            self.error, self.notice = "", ""
            settings = (self.provider, self.model.strip(), self.base_url.strip())
            typed = self._typed_key.strip()
            baseline = self._baseline
        try:
            await write_llm_settings(settings, typed, baseline)
        except SettingsChangedElsewhere:
            # Not `_failed`: that would leave the stale values on screen, and
            # the second press would carry the baseline this re-read installs
            # and succeed — quietly doing the damage the refusal prevented.
            logger.warning("Refused a save from a form somebody else had moved on")
            await self._adopt(SETTINGS_MOVED, as_error=True)
            return
        except ApiKeyNotStored as error:
            # Never `logger.exception`: the traceback would carry the
            # `StatementError` this was raised from, and that one quotes the
            # key. `ApiKeyNotStored` is the stripped message and the only one
            # safe to record.
            logger.error("The interpreter API key could not be stored: %s", error)
            await self._failed(KEY_NOT_STORED)
            return
        except Exception:
            logger.exception("Could not store the interpreter settings")
            await self._failed(SAVE_FAILED)
            return
        async with self:
            self._typed_key = ""
        logger.info("Interpreter settings saved from the settings page")
        await self._adopt(SAVED)

    @rx.event(background=True)
    async def clear_api_key(self) -> None:
        """Forget the stored key — the explicit control a write-only key needs.

        Its own button because an empty box means "unchanged": without this
        there is no way back from "a key is stored" except typing another one,
        and no way at all to stop sending a third party a credential that
        should have been revoked.
        """
        async with self:
            if self.saving:
                return
            self.saving = True
            self.error, self.notice = "", ""
        try:
            await clear_llm_api_key()
        except Exception:
            logger.exception("Could not clear the interpreter API key")
            await self._failed(SAVE_FAILED)
            return
        await self._adopt(KEY_CLEARED)

    @rx.event(background=True)
    async def use_configuration_file(self) -> None:
        """Forget everything stored here and let the file answer again.

        The other direction of the whole feature. Every stored column is
        nullable and ``NULL`` means "not set", so this writes three nulls and
        drops the key — after which the effective configuration is exactly what
        it was before anybody opened this page. Without it a single save would
        be one-way.
        """
        async with self:
            if self.saving:
                return
            self.saving = True
            self.error, self.notice = "", ""
        try:
            await reset_llm_settings()
        except Exception:
            logger.exception("Could not clear the stored interpreter settings")
            await self._failed(SAVE_FAILED)
            return
        logger.info("The stored interpreter settings were cleared")
        await self._adopt(USING_THE_FILE)

    async def _adopt(self, said: str, *, as_error: bool = False) -> None:
        """Make the running application re-read, then say how it went.

        The reload is the composition root's and is allowed to fail on its own
        — the row is written either way — so a failure here reports that the
        save landed and the application has not picked it up, which is a
        different sentence from either success or failure.
        """
        try:
            current = await llm_control().reload()
            stored, baseline = await stored_llm_baseline()
        except Exception:
            logger.exception("Could not adopt the stored interpreter settings")
            await self._failed(ADOPT_FAILED)
            return
        async with self:
            self._apply(current, stored, baseline)
            self.saving = False
            if as_error:
                self.error, self.notice = said, ""
            else:
                self.error, self.notice = "", said

    async def _failed(self, said: str) -> None:
        """Leave the form as it stands and say what went wrong."""
        async with self:
            self.saving = False
            self.error, self.notice = said, ""


USING_THE_FILE = (
    "The stored interpreter settings were cleared. The configuration file and "
    "the environment answer again."
)

ADOPT_FAILED = (
    "The settings were stored, but the running application did not pick them "
    "up. Restart it, or open this page again."
)
