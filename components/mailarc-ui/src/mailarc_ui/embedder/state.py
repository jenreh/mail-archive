"""Setting the embedder from a browser, including the key that opens it.

The one page in this application that *writes* configuration, and it writes the
only secret a user types after the mail credentials. Three rules follow from
that, and all three are held here rather than in the components — a Reflex
event handler is addressable by name over the websocket, so nothing a template
does can be relied on to protect anything.

**The API key is write-only, structurally.** Nothing here ever asks for a
stored key. What the form needs to know is whether one exists, and that is
``api_key_is_set`` — an ``IS NOT NULL`` the database evaluates, so what crosses
the connection is a boolean and the ciphertext is neither fetched nor
decrypted. The only method here that touches the column at all writes it.
(``store`` does load the row inside its own session, because an ORM ``UPDATE``
has to; this module discards the entity it hands back rather than reading a
field off it — which is the difference between a key existing for a moment
inside a repository and a key reaching a state var.)

What a human types goes into :attr:`EmbedderSettingsState._typed_key`, a
backend-only var Reflex does not ship to a browser — the same device
``MailAccountState._typed`` uses for a mail password — and is dropped the
moment the write succeeds. An empty box on save leaves the stored key alone,
because :meth:`~mailarc_core.database.repositories.SemanticSettingsRepository.store`
has no ``api_key`` parameter to pass it through; forgetting a key is
:meth:`EmbedderSettingsState.clear_api_key`, its own control saying its own
sentence.

**The form shows what is in force, not what is stored.** The effective
configuration is the file, the environment and the stored row merged, and only
the composition root can perform that merge (§4.1) — so it arrives through
:class:`~mailarc_analytics.semantic.config.SemanticControl`, read out of the
service registry inside a method the way every other configuration reaches
``mailarc-ui``. A form pre-filled from the stored row alone would show four
empty boxes on an installation configured in ``config.yaml``, and the first
save would overwrite that configuration with blanks.

Saving is not the end of the work: the same control re-reads the store and
rebuilds the embedder, so a change takes effect in the running application
rather than at the next restart. The *worker* is the exception and the form
says so — it reads these settings once when it starts, and the embed job runs
there.

**And the page can start that job.** Until it could, the warnings above named
``task graph:embed`` as their remedy, which is a shell command in a form — and
on the desktop bundle a command with no terminal to type it into.
:meth:`EmbedderSettingsState.start_embed` queues an ``embed`` job and follows
the row, modelled on
:meth:`~mailarc_ui.insights.state.AnalyticsInsightsState.start_rebuild` down to
:meth:`EmbedderSettingsState._adopt_open_embed`, so that a second tab follows
the running job instead of queueing a second one.

What is *not* here is anything the page says. Every message lives in
:mod:`mailarc_ui.embedder.model` beside the readings they are said about; what
is left in this module is everything that touches a session, the registry, the
job queue or the state lock.
"""

import asyncio
import contextlib
import logging
from datetime import datetime

import reflex as rx
from appkit_commons.database.session import get_asyncdb_session
from reflex.event import EventCallback

from mailarc_analytics.semantic import (
    NATIVE_DIMENSIONS,
    SemanticProvider,
    native_dimension,
)
from mailarc_core.database.repositories import (
    ApiKeyNotStored,
    SettingsChangedElsewhere,
)
from mailarc_sync.jobs import JobKind, JobQueue
from mailarc_ui.embedder.model import (
    EMBED_CANCEL_ASKED,
    EMBED_CANCEL_TOOK_EFFECT,
    EMBED_RUNNING,
    KEY_CLEARED,
    KEY_NOT_STORED,
    LOAD_FAILED,
    NO_ADVICE,
    NO_EMBED_JOB,
    NO_EMBEDDER_TO_RUN,
    REINDEX_FAILED,
    RESET,
    SAVE_FAILED,
    SAVED,
    SAVED_NOT_ADOPTED,
    SAVED_NOT_SHOWN,
    SETTINGS_MOVED,
    UNSAVED_BEFORE_EMBED,
    Advice,
    EmbedderReading,
    EmbedJobView,
    gave_up_on,
    host_advice,
    index_advice,
    key_status,
    reindexed,
    vector_advice,
)
from mailarc_ui.embedder.reads import (
    SETTINGS,
    from_the_graph,
    is_absolute_http,
    semantic_control,
    stored_baseline,
    write_settings,
)
from mailarc_ui.kit import FieldErrors

logger = logging.getLogger(__name__)


POLL_TICKS_ALLOWED = 900
"""Half an hour at the default two-second interval.

The bound :class:`~mailarc_ui.insights.state.AnalyticsInsightsState` needed for
the same reason: a loop whose only exits are "the job ended" and "the job
vanished" has no exit at all when no worker is running, which is the normal
state of a dev machine and of any install where the worker is not up. Giving up
is only ever a statement about this page — the job itself is untouched.

A number and not a sentence, which is why it stays here while every message
this module assigns lives in :mod:`mailarc_ui.embedder.model`: it is a property
of the loop below, and the loop is the thing that would otherwise never end.
"""


def _said_about(error: Exception, otherwise: str = LOAD_FAILED) -> str:
    """What a human reads when a read failed — never the driver's own words.

    :data:`SAVE_FAILED` refuses to quote SQLAlchemy because its message carries
    the statement, the bind parameters and, for a SQLite archive opened by
    path, the store's location — and this string is rendered into a browser.
    The load path used to render ``str(error)`` regardless, one method away.

    :data:`NO_CONTROL` is the one passthrough, narrowed to ``RuntimeError``
    because that is the only exception this module raises itself.
    """
    if isinstance(error, RuntimeError):
        return str(error) or otherwise
    return otherwise


DIMENSION_FIELD = "dimension"
BASE_URL_FIELD = "base_url"
"""The two settings this form has a rule for, named where the rules are."""

DIMENSION_TOO_SMALL = "At least one float per vector."
"""A stored zero is not a smaller index — it is an embedder that can never
write a vector the graph accepts, and the composition root drops the whole row
over it."""

NOT_AN_HTTP_URL = "An absolute http:// or https:// URL, or empty."
"""What the box says instead of swallowing the keystroke.

This field decides which host receives the archive's stored bearer token on
every embedding call, so it used to refuse to hold anything that was not
already a complete URL — which meant it could not be typed into at all: ``h``
is not an absolute URL, so the first keystroke was discarded along with every
one after it. The check now runs where it matters, at the write, and the box
says what it wants while somebody is still typing it. See
:meth:`EmbedderSettingsState.save`.
"""


class EmbedderSettingsState(FieldErrors, rx.State):
    """The embedder form: what is in force, what to change it to, and the cost.

    The four settable values are ordinary vars and the key is not one of them.
    :attr:`in_force` is the reading the last load produced and is what the two
    advice vars compare against — separate from the editable fields on purpose,
    because "what would this change" cannot be answered by a form that only
    knows its own current contents.
    """

    provider: str = SemanticProvider.NONE.value
    model: str = ""
    dimension: int = 0
    base_url: str = ""

    api_key_stored: bool = False
    """Whether a key is stored *here*, which is the only one Clear can forget."""

    api_key_in_force: bool = False
    """Whether a key reaches the embedder at all, from wherever it comes.

    Both booleans and neither a value; see
    :attr:`~mailarc_ui.embedder.model.EmbedderReading.api_key_in_force`.
    """

    in_force: EmbedderReading = EmbedderReading()
    """The embedder as the last load found it, vectors counted where possible.

    Not a copy of the fields above: those move as somebody types, this does
    not, and the difference between the two is exactly what the warnings are
    about.
    """

    reindexing: bool = False
    """Whether an index rebuild is running, so the button cannot be pressed twice.

    Its own flag rather than reusing ``saving``: a rebuild takes as long as the
    graph takes to drop and build an HNSW index, and a form that looked like it
    was saving for that whole time would say the wrong thing about what is
    happening to the archive.
    """

    loading: bool = True
    saving: bool = False
    error: str = ""
    notice: str = ""

    _typed_key: str = ""
    """The API key as it is being typed, and the only place it exists.

    Backend-only — Reflex ships no var whose name starts with an underscore —
    for the reason ``MailAccountState._typed`` is: a secret has no business
    being sent back to the browser it came from. Cleared as soon as a write
    succeeds, so it does not outlive the save that used it.
    """

    _baseline: datetime | None = None
    """The stored row's timestamp as this form last read it, or ``None``.

    Backend-only, like :attr:`_typed_key`, because a browser has no use for it:
    it exists so that a save can say "if the row is still the one I read", and
    nothing on screen is decided by it. See
    :class:`~mailarc_core.database.repositories.SettingsChangedElsewhere`.
    """

    job_id: int = 0
    """The vector rebuild this page is following, or zero for none."""

    job: EmbedJobView = NO_EMBED_JOB
    """The last reading of it.

    Separate from :attr:`job_id` for the reason the import and insights panels
    keep the two apart: a read that comes back empty must not make the control
    forget what it was following.
    """

    embed_message: str = ""
    starting: bool = False
    cancelling: bool = False
    polling: bool = False
    poll_interval: int = 2
    poll_ticks_allowed: int = POLL_TICKS_ALLOWED

    @rx.var
    def can_reindex(self) -> bool:
        """Whether the index may be rebuilt right now.

        Refused while anything else is in flight, and refused outright without
        a length: rebuilding at zero would drop the index and build nothing,
        which :func:`~mailarc_analytics.semantic.indexing.rebuild_index`
        refuses anyway — this is the same refusal said before the click rather
        than after it.
        """
        return (
            not self.loading
            and not self.saving
            and not self.reindexing
            and not self.starting
            and self.dimension > 0
        )

    @rx.var
    def key_status(self) -> str:
        """What the form says about the key, which is never its value."""
        return key_status(self.in_force)

    @rx.var
    def key_pending(self) -> bool:
        """Whether saving would replace the stored key.

        A boolean over the typed value rather than the value: the form has to
        be able to say "this save writes a new key" without the key crossing
        the wire to say it.
        """
        return self._typed_key.strip() != ""

    @rx.var
    def key_matters(self) -> bool:
        """Whether the chosen provider uses a key at all. Ollama ignores one."""
        return self.provider == SemanticProvider.OPENAI.value

    @rx.var
    def key_missing(self) -> bool:
        """OpenAI is chosen and no key reaches it — a 401 waiting to happen.

        Over :attr:`api_key_in_force`: an archive keyed from ``config.yaml``
        has no stored row and no 401 either.
        """
        return self.key_matters and not self.api_key_in_force

    @rx.var
    def blocked(self) -> bool:
        """Whether every control is dead, which is the state before a load.

        Its own var rather than :attr:`loading` at each call site, because the
        two answer different questions: one is "a read is out", the other is
        "there is nothing here to change yet". They coincide today and the
        components read this one, so what a control is disabled *for* stays
        stated in one place.
        """
        return self.loading

    @rx.var
    def can_clear_key(self) -> bool:
        """Whether there is a stored key to forget."""
        return self.api_key_stored and not self.saving

    @rx.var
    def vector_advice(self) -> Advice:
        """What changing the embedder identity would cost, if anything."""
        if self.loading:
            return NO_ADVICE
        return vector_advice(
            self.in_force, provider=self.provider, model=self.model.strip()
        )

    @rx.var
    def index_advice(self) -> Advice:
        """What the typed vector length costs against the graph's own index."""
        if self.loading:
            return NO_ADVICE
        return index_advice(self.in_force, dimension=self.dimension)

    @rx.var
    def host_advice(self) -> Advice:
        """What moving the embedding API somewhere else costs, key included."""
        if self.loading:
            return NO_ADVICE
        return host_advice(
            self.in_force,
            provider=self.provider,
            base_url=self.base_url,
            keyed=self.api_key_in_force or self.key_pending,
        )

    @rx.var
    def can_save(self) -> bool:
        """Whether pressing Save could produce anything.

        A dimension of zero is refused here as well as in the merge: a stored
        zero is not a smaller index, it is an embedder that can never write a
        vector the graph accepts, and the composition root would drop the whole
        row over it.

        ``has_errors`` rather than a second copy of each rule — the fields have
        already worked out whether they are happy, and a button that decided
        for itself would be the place the two answers drift apart.
        """
        return not self.loading and not self.saving and not self.has_errors

    @rx.var
    def embedder_configured(self) -> bool:
        """Whether the embedder *in force* could compute a vector at all.

        Over :attr:`in_force` and not over :attr:`provider`, and the difference
        is the whole point: the worker reads what is stored, so a form showing
        ``ollama`` in an unsaved box is not an archive that can be embedded.
        """
        return self.in_force.provider not in ("", SemanticProvider.NONE.value)

    @rx.var
    def settings_unsaved(self) -> bool:
        """Whether the form holds a change a rebuild would not embed with.

        The four editable values against the reading they were loaded from.
        ``model`` and ``base_url`` are compared stripped because that is what a
        save would store, so trailing whitespace is not a difference.
        """
        return (
            self.provider != self.in_force.provider
            or self.model.strip() != self.in_force.model
            or self.dimension != self.in_force.dimension
            or self.base_url.strip() != self.in_force.base_url
        )

    @rx.var
    def has_message(self) -> bool:
        """Whether the last action left anything to say.

        Read by :func:`~mailarc_ui.embedder.components.message_alerts` to decide
        whether to render *at all*. A block of two empty alerts is not empty in
        the layout: its own gap survives its children, and the panel's gap
        survives the block, so a page with nothing to report opened thirty
        pixels lower than every other page in the application.
        """
        return bool(self.error or self.notice)

    @rx.var
    def has_embed_job(self) -> bool:
        return self.job.job_id > 0

    @rx.var
    def can_embed(self) -> bool:
        """Whether pressing Rebuild the vectors could produce anything."""
        return (
            self.embedder_configured
            and not self.job.active
            and not self.starting
            and not self.loading
        )

    @rx.var
    def can_cancel_embed(self) -> bool:
        return self.job.active and not self.job.cancel_requested

    @rx.event(background=True)
    async def load(self) -> None:
        """Read what is in force and what it has embedded. The page's ``on_load``.

        Background, because one of the three reads is a count over every
        message in the archive. A plain handler would hold this client's state
        lock for it and freeze every other event on the session behind a
        settings page.
        """
        async with self:
            self.loading = True
            self.error = ""
        try:
            baseline, reading = await self._read()
        except Exception as error:
            logger.exception("Could not read the embedder settings")
            async with self:
                self.loading = False
                self.error = _said_about(error)
            return
        async with self:
            self._apply(baseline, reading)

    @rx.event
    async def set_provider(self, value: str) -> None:
        """Choose an embedder, without trusting the string that named it.

        ``SemanticProvider(value)`` would raise on anything the selector did
        not produce, and this arrives over the socket where an event's
        arguments are whatever the caller sent. An unknown name changes
        nothing, which is the only answer that cannot make the form claim
        something the archive will not do.
        """
        if value not in {one.value for one in SemanticProvider}:
            logger.warning("Ignoring an unknown embedding provider")
            return
        self.provider = value
        # The length follows the provider, because it is not guessable and
        # getting it wrong fails silently: a vector of the wrong length is
        # accepted, stored and never indexed, so search finds nothing rather
        # than failing. Ollama's model is 768 and cannot be asked for fewer;
        # OpenAI's are 1536 and 3072 and can. Overwriting whatever was in the
        # box is right here and only here — a length chosen for the *previous*
        # provider says nothing about this one.
        offered = native_dimension(SemanticProvider(value), self.model)
        if offered is not None:
            self.dimension = offered

    @rx.event
    async def set_model(self, value: str) -> None:
        """The model's name; empty means whatever the provider ships.

        Naming a model whose length is known offers that length — the number
        belongs to the model rather than to the vendor, and one OpenAI account
        ships both 1536 and 3072. A model nobody here has heard of leaves the
        length alone: inventing one would be a guess, and a length set by hand
        is a real choice for the ``text-embedding-3-*`` models, which can be
        asked for fewer floats than they natively produce.
        """
        was = self.model
        self.model = value
        if value.strip() == was.strip():
            return
        known = NATIVE_DIMENSIONS.get(value.strip())
        if known is None:
            return
        # Only overwrite a length the form itself offered. If what is in the
        # box is still the length the previous choice suggested, nobody has
        # touched it and this is an offer replacing an offer. If it differs,
        # somebody typed it, and for `text-embedding-3-*` a shorter vector is a
        # real choice — the model produces a prefix that is itself usable, and
        # a 768 index against a 1536-native model is a legitimate way to run.
        # Re-offering the native length there would quietly undo the decision.
        offered = native_dimension(SemanticProvider(self.provider), was)
        if self.dimension in (0, offered):
            self.dimension = known

    @rx.event
    async def set_dimension(self, value: float | str) -> None:
        """Floats per vector, from a control that sends a number or ``""``.

        Mantine's NumberInput hands back the raw value, so an emptied box
        arrives as an empty string and a half-typed one as something ``int``
        refuses. Zero is a legal thing to hold — it is what an empty box means
        — and an illegal thing to save, which :attr:`can_save` is where it is
        refused.
        """
        if value == "":
            self.dimension = 0
            self._validate_dimension()
            return
        with contextlib.suppress(ValueError):
            self.dimension = int(float(value))
        self._validate_dimension()

    @rx.event
    async def set_base_url(self, value: str) -> None:
        """Where the embedding API lives; empty means the provider's own.

        Held as typed and checked as a field, rather than discarded on the
        keystroke. This is the value that decides which host receives the
        stored bearer token on every call, so it *is* checked — but at the
        write, in :meth:`save`, which is the boundary the guarantee is about.
        Refusing it here as well only meant the box could never be typed into:
        ``h`` is not an absolute URL either. See :data:`NOT_AN_HTTP_URL`.
        """
        self.base_url = value
        self._validate_base_url()

    @rx.event
    async def set_api_key(self, value: str) -> None:
        """Hold the typed key server-side, and nowhere else.

        Straight into the backend-only var: it is never echoed into a public
        var, never logged, and never read back out of the database once
        written.
        """
        self._typed_key = value

    # ── What makes this form valid ───────────────────────────────────────

    def _validate_dimension(self) -> bool:
        """A length the graph could actually index."""
        return self._check(
            DIMENSION_FIELD, "" if self.dimension > 0 else DIMENSION_TOO_SMALL
        )

    def _validate_base_url(self) -> bool:
        """Empty, or somewhere ``httpx`` can actually send a request."""
        typed = self.base_url.strip()
        wrong = bool(typed) and not is_absolute_http(typed)
        return self._check(BASE_URL_FIELD, NOT_AN_HTTP_URL if wrong else "")

    def _validate_settings(self) -> bool:
        """Both rules, for a press that is about to write."""
        return all([self._validate_dimension(), self._validate_base_url()])

    @rx.event(background=True)
    async def save(self) -> None:
        """Write the settings, then make the running application adopt them.

        One transaction for both halves — the four settings and the key — so
        the one way the key write can fail takes the settings with it rather
        than leaving a row describing an embedder whose credentials were never
        stored.

        The adoption is a second step and is allowed to fail on its own: the
        row is written either way, and what the two outcomes say differs
        because a form that reported "Saved" over an application still using
        the previous embedder would send somebody looking for the fault in the
        model.
        """
        async with self:
            if self.saving:
                return
            # Where the base-URL guarantee actually lives. The setter marks the
            # field so somebody can see what is wrong while they type; *this*
            # is what keeps a value that is not an absolute http(s) URL from
            # reaching `write_settings`, and it runs whatever the button was
            # doing — `can_save` is a UI gate and this is the boundary.
            if not self._validate_settings():
                return
            self.saving = True
            self.error, self.notice = "", ""
            settings = (
                self.provider,
                self.model.strip(),
                self.dimension,
                self.base_url.strip(),
            )
            typed = self._typed_key.strip()
            baseline = self._baseline
        try:
            await write_settings(settings, typed, baseline)
        except SettingsChangedElsewhere:
            # Not `_failed`: that leaves the stale values on screen, and the
            # second press would carry the baseline this re-read installs and
            # succeed — quietly doing the damage the refusal just prevented.
            logger.warning("Refused a save from a form somebody else had moved on")
            await self._adopt(SETTINGS_MOVED, as_error=True)
            return
        except ApiKeyNotStored as error:
            # Never `logger.exception` here: the traceback would carry the
            # `StatementError` this was raised from, and that one quotes the
            # key. `ApiKeyNotStored` is the stripped message and the only one
            # safe to record.
            logger.error("The embedder API key could not be stored: %s", error)
            await self._failed(KEY_NOT_STORED)
            return
        except Exception:
            logger.exception("Could not store the embedder settings")
            await self._failed(SAVE_FAILED)
            return
        async with self:
            self._typed_key = ""
        logger.info("Embedder settings saved from the settings page")
        await self._adopt(SAVED)

    @rx.event(background=True)
    async def clear_api_key(self) -> None:
        """Forget the stored key — the explicit control a write-only key needs.

        Its own button because an empty box means "unchanged": without this
        there would be no way back from "a key is stored" except typing another
        one, and no way at all to stop sending a third party a credential that
        should have been revoked.
        """
        async with self:
            if self.saving:
                return
            self.saving = True
            self.error, self.notice = "", ""
        try:
            async with get_asyncdb_session() as session:
                await SETTINGS.clear_api_key(session)
        except Exception:
            logger.exception("Could not clear the embedder API key")
            await self._failed(SAVE_FAILED)
            return
        await self._adopt(KEY_CLEARED)

    @rx.event(background=True)
    async def rebuild_index(self) -> None:
        """Rebuild the vector index at the length now in force.

        The operation that makes the length a real setting. A vector index has
        one fixed length, fixed when it is built, so choosing a model of a
        different size leaves an index that accepts every vector written
        against it, indexes none of them, and reports nothing — the failure
        this whole page exists to keep in front of a human.

        Destructive in one specific way, and the notice says so rather than
        the docstring alone: every stored vector is forgotten, because one of
        the old length in an index of the new one is worse than none. The embed
        job afterwards recomputes them, which is why the notice names it.
        Nothing a message arrived with is touched.
        """
        async with self:
            if self.saving or self.reindexing:
                return
            self.reindexing = True
            self.error, self.notice = "", ""
        try:
            cleared = await semantic_control().reindex()
        except Exception:
            logger.exception("Could not rebuild the vector index")
            async with self:
                self.reindexing = False
            await self._failed(REINDEX_FAILED)
            return
        async with self:
            self.reindexing = False
        logger.info("Vector index rebuilt; %d vector(s) cleared", cleared)
        await self._adopt(reindexed(cleared))

    @rx.event(background=True)
    async def use_configuration_file(self) -> None:
        """Forget everything stored here and let the file answer again.

        The other direction of the whole feature. Every stored column is
        nullable and ``NULL`` means "not set", so this writes four nulls and
        drops the key — after which the effective configuration is exactly what
        it was before anybody opened this page. Without it a single save would
        be one-way: an administrator who configures the archive in
        ``config.yaml`` could never get back to it from the browser.
        """
        async with self:
            if self.saving:
                return
            self.saving = True
            self.error, self.notice = "", ""
        try:
            async with get_asyncdb_session() as session:
                await SETTINGS.store(
                    session, provider=None, model=None, dimension=None, base_url=None
                )
                await SETTINGS.clear_api_key(session)
        except Exception:
            logger.exception("Could not forget the stored embedder settings")
            await self._failed(SAVE_FAILED)
            return
        logger.info("The stored embedder settings were forgotten")
        await self._adopt(RESET)

    @rx.event
    async def start_embed(self) -> EventCallback[()] | None:
        """Queue a vector rebuild and start following it.

        Modelled on
        :meth:`~mailarc_ui.insights.state.AnalyticsInsightsState.start_rebuild`,
        down to the adoption below, because the two controls have the same
        problem: ``job_id`` is per page and starts at zero, so "is one already
        running?" cannot be answered from this state alone.

        No account: an embed job is about the whole archive, which is why it
        carries no ``account_id`` — and why :meth:`_adopt_open_embed` looks for
        an open job with none.
        """
        if not self.embedder_configured:
            self.embed_message = NO_EMBEDDER_TO_RUN
            return None
        await self._sync_job()
        if not self.job.active:
            await self._adopt_open_embed()
        if self.job.active:
            self.embed_message = EMBED_RUNNING
            return self._follow()
        self.starting = True
        try:
            self.job_id = await self._queue().enqueue(JobKind.EMBED)
        except Exception as error:
            logger.exception("Could not queue a vector rebuild")
            self.embed_message = f"The rebuild could not be queued: {error}"
            return None
        finally:
            self.starting = False
        # Said on the way in rather than after the fact: the worker reads the
        # stored settings, and a reader who has just been warned that a model
        # change invalidates every vector is one click from re-embedding the
        # archive with the model they were replacing.
        self.embed_message = UNSAVED_BEFORE_EMBED if self.settings_unsaved else ""
        await self._sync_job()
        logger.info("Started vector rebuild job %d", self.job_id)
        return self._follow()

    @rx.event
    async def cancel_embed(self) -> None:
        """Ask the rebuild to stop — a flag, not a kill (§7.2).

        The worker reads it between batches, so what is in the graph is whole
        batches of vectors. That is not a half-written state needing repair: a
        run only ever looks for messages with no vector under the model in
        force, so the next one continues from exactly where this stopped.
        """
        if self.job_id <= 0:
            return
        self.cancelling = True
        try:
            asked = await self._queue().request_cancel(self.job_id)
        finally:
            self.cancelling = False
        if not asked:
            self.embed_message = "That rebuild had already ended."
            await self._sync_job()
            return
        await self._sync_job()
        self.embed_message = (
            EMBED_CANCEL_ASKED if self.job.active else EMBED_CANCEL_TOOK_EFFECT
        )

    @rx.event
    def stop_polling(self) -> None:
        """Stop following the rebuild — the page is going away.

        Wired to ``embedder_panel``'s ``on_unmount``: without it a user who
        navigates away during a rebuild leaves a background task hitting the
        database for the life of the session, one per abandoned page.
        """
        self.polling = False

    @rx.event(background=True)
    async def poll(self) -> None:
        """Follow the rebuild to its end, then re-read what is embedded.

        The lock is held around the state mutations and never around the read
        or the sleep, so a rebuild that takes an hour never blocks the rest of
        the app. Every way out of the loop is an end: the flag going down, the
        job reaching a final state, the job disappearing from the queue, and
        the tick bound. A dropped read is the one thing that is not an end; the
        next tick asks again.

        Succeeded, failed and cancelled all re-read the count, because all
        three change it — a run that stopped halfway still embedded everything
        up to the batch it stopped on, and the warnings above compare against
        that number.
        """
        ticks = 0
        while True:
            async with self:
                if not self.polling or self.job_id <= 0:
                    self.polling = False
                    return
                if ticks >= self.poll_ticks_allowed:
                    self.polling = False
                    self.embed_message = gave_up_on(self.job)
                    logger.info(
                        "Stopped following vector rebuild job %d after %d ticks",
                        self.job_id,
                        ticks,
                    )
                    return
                job_id = self.job_id
            ticks += 1
            dropped = False
            try:
                reading = await self._read_job(job_id)
            except Exception:
                logger.exception("Vector rebuild job poll failed")
                reading, dropped = None, True
            async with self:
                if not self.polling:
                    return
                if not dropped:
                    self.job = reading or NO_EMBED_JOB
                    if not self.job.active:
                        logger.info(
                            "Vector rebuild job %d ended as %s", job_id, self.job.status
                        )
                        self.polling = False
                        break
            await asyncio.sleep(self.poll_interval)
        await self._recount()

    async def _recount(self) -> None:
        """Re-read the coverage without touching what somebody is typing.

        Emphatically not :meth:`_apply`. That one writes the four editable
        fields back from the reading, which is right after a save and wrong
        here: a rebuild takes minutes, the form is editable throughout, and a
        job finishing must not silently revert the model somebody is halfway
        through changing. What the end of a run actually changes is
        :attr:`in_force`'s vector count — the number the warnings above compare
        against — so that is the only thing this writes.
        """
        try:
            baseline, reading = await self._read()
        except Exception:
            logger.exception("Could not re-read the embedder settings after a rebuild")
            return
        async with self:
            self.in_force = reading
            # The row's timestamp moves with it, so a save after a rebuild is
            # not refused for a change this page made to itself.
            self._baseline = baseline

    async def _adopt_open_embed(self) -> None:
        """Take over a rebuild somebody else queued, rather than adding one.

        :meth:`~mailarc_ui.insights.state.AnalyticsInsightsState._adopt_open_rebuild`
        for the embed kind. A second tab, or the same tab after a reload, knows
        about no job and would queue its own; two embed runs against one archive
        are not destructive the way two derives are — nothing is deleted first —
        but they are two workers computing the same vectors and, on ``openai``,
        paying twice to upload the same archive.

        A queue that cannot answer is not a reason to refuse: the enqueue right
        after would fail too, and that path already has the message for it.
        """
        try:
            open_job = await self._queue().find_open(JobKind.EMBED)
        except Exception:
            logger.exception("Could not ask the queue for an open vector rebuild")
            return
        if open_job is None:
            return
        self.job = EmbedJobView.from_job(open_job)
        self.job_id = self.job.job_id
        logger.info("Following vector rebuild job %d, queued elsewhere", self.job_id)

    def _follow(self) -> EventCallback[()] | None:
        """Start watching the rebuild, unless this page already is."""
        if self.polling:
            return None
        self.polling = True
        return EmbedderSettingsState.poll

    def _queue(self) -> JobQueue:
        """Built per call, never at import.

        The session factory it defaults to is configured while the app starts;
        a queue built at module level would capture the world too early.
        """
        return JobQueue()

    async def _read_job(self, job_id: int) -> EmbedJobView | None:
        """The watched job as this control shows it, or nothing if it is gone."""
        job = await self._queue().get(job_id)
        return None if job is None else EmbedJobView.from_job(job)

    async def _sync_job(self) -> None:
        """One read of the watched job, applied — for everything but the poll.

        A read that fails leaves the last reading standing rather than blanking
        the control: a database hiccup is not news that the rebuild ended.
        """
        if self.job_id <= 0:
            return
        try:
            self.job = await self._read_job(self.job_id) or NO_EMBED_JOB
        except Exception:
            logger.exception("Could not read vector rebuild job %d", self.job_id)

    async def _adopt(self, said: str, *, as_error: bool = False) -> None:
        """Make the write take effect, then show the form what is now in force.

        Re-reading afterwards is not tidiness: :attr:`in_force` is what the two
        warnings compare against, so a form that saved without re-reading would
        go on warning about a change it had already made.
        """
        adopted = True
        try:
            await semantic_control().reload()
        except Exception:
            logger.exception("The embedder settings were saved but not adopted")
            adopted = False
        try:
            baseline, reading = await self._read()
        except Exception as error:
            logger.exception("Could not re-read the embedder settings")
            async with self:
                self.saving = False
                # Not LOAD_FAILED: that one promises nothing was changed, and
                # here the write landed and only the read back did not.
                self.error = _said_about(error, otherwise=SAVED_NOT_SHOWN)
            return
        async with self:
            self._apply(baseline, reading)
            if as_error:
                self.error = said
                return
            self.notice = said if adopted else SAVED_NOT_ADOPTED

    async def _failed(self, said: str) -> None:
        """One write's failure, applied — the form keeps what it was showing."""
        async with self:
            self.saving = False
            self.error = said

    async def _read(self) -> tuple[datetime | None, EmbedderReading]:
        """The embedder in force, whether a key is stored, and what is embedded.

        The coverage count is the only one of the three allowed to fail
        quietly. It is the only one that touches the graph, a settings page has
        to work on an installation whose graph is not running — configuring the
        embedder is something you do *before* it works — and a missing count
        produces a stronger warning rather than a weaker one.
        """
        config = semantic_control().current()
        stored_key, baseline = await stored_baseline()
        embedded, index, known = await from_the_graph()
        return baseline, EmbedderReading(
            provider=str(config.provider),
            model=config.model,
            dimension=config.dimension,
            base_url=config.base_url,
            api_key_stored=stored_key,
            # `is not None` on the merged config this method already holds:
            # every other field shows the effective value and the key's
            # presence is no different, while the value never leaves the store.
            api_key_in_force=config.api_key is not None,
            embedded=embedded,
            coverage_known=known,
            index_dimension=index,
            index_known=known,
        )

    def _apply(self, baseline: datetime | None, reading: EmbedderReading) -> None:
        """One reading, all of it at once, into the form and its baseline.

        ``baseline`` is not shown anywhere; it is what the next save quotes
        back so that a row somebody else moved on is refused rather than
        overwritten. It has to be replaced on *every* apply, including after a
        successful save, or the form would work exactly once.
        """
        self._baseline = baseline
        self.in_force = reading
        self.provider = reading.provider
        self.model = reading.model
        self.dimension = reading.dimension
        self.base_url = reading.base_url
        self.api_key_stored = reading.api_key_stored
        self.api_key_in_force = reading.api_key_in_force
        # A reload is a request for the stored truth and a half-typed secret is
        # not part of it. Without this, a key pasted in and thought better of
        # survived the Reload meant to undo it and was written by the next
        # unrelated save — and the password box is uncontrolled, so nothing in
        # the DOM contradicted the impression of a clean form.
        self._typed_key = ""
        self.loading = False
        self.saving = False
