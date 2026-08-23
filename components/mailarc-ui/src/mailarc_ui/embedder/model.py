"""What this page reads, and every sentence it says.

No I/O, no Reflex and no registry: everything here is a value, a projection or
a function over values. That is what lets the two warnings this page exists for
be checked against a table of cases — a change with vectors behind it, a change
with none, a change nobody can count — instead of against a browser.

The warnings are the point of the page as much as the form is. §7.4's finding
is that a dimension the vector index does not carry fails *silently*: the graph
accepts the vector, stores it and does not index it, so the archive goes on
answering searches with a shrinking fraction of itself and nothing anywhere
says so. A form that let somebody change the model without reading that
sentence would be a worse thing than no form.

**Every message the page can show is here, not beside the handler that assigns
it**, and that is the seam this module is split on — the same one
:mod:`mailarc_ui.insights.model` is split on, for the same reason. The two
halves are wrong in different ways. A sentence is wrong when it names a remedy
the reader cannot reach, tells them a rebuild is running when the button is
live, or explains a save while they are looking at a cancel; a state is wrong
when it holds a lock across an await, blanks a control on a dropped read, or
reverts a half-typed model because a job finished. Checking the first needs no
session, no queue and no event loop — a value in, a string out — and while both
halves shared a file that was true and impossible to see.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from mailarc_analytics.semantic import SemanticProvider
from mailarc_sync.jobs import JobState, SyncJob
from mailarc_ui.imports import percent_of

EMBED_REMEDY = "the Rebuild the vectors button below"
"""How an embed job is started, named as the thing a reader can actually do.

It used to name ``task graph:embed``, and had to: nothing in the application
queued an ``embed`` job, so a terminal was the only way to compute a vector and
a sentence that said "re-run the embed job" was one a reader could not act on.
On the desktop bundle it was worse than unhelpful — there is no terminal into
the application's environment there at all, so the remedy for the warning this
page exists to give named something the reader could not reach.

:meth:`~mailarc_ui.embedder.state.EmbedderSettingsState.start_embed` is what
closed that, and this sentence points at it. The task still exists and still
works; it is simply no longer the only way, and naming a shell command inside a
form is the wrong instruction to give somebody who is already looking at the
button.
"""

_NO_PERCENT = "—"
"""Stands in while a job has not reported a batch yet.

A second one beside the insights panel's rather than a shared constant, for the
reason its ``_STATE_COLORS`` is a second map: what a panel prints in place of a
number it does not have is that panel's decision, and sharing it would tie two
controls' looks together because both happen to render a job.
"""

_STATE_COLORS = {
    JobState.QUEUED: "gray",
    JobState.RUNNING: "blue",
    JobState.SUCCEEDED: "teal",
    JobState.FAILED: "red",
    JobState.CANCELLED: "orange",
}
"""What each job state looks like on this control."""

NO_MODEL = "the provider's own default model"
"""What an empty model name means, spelled out.

``SemanticConfig.model`` reads ``""`` as "whatever this provider ships as its
default", and a warning that named the old embedder as ``ollama /`` with
nothing after the slash would look like a bug rather than like a setting.
"""


class Advice(BaseModel):
    """One sentence the form shows about a change, and how loudly.

    Text and colour together because the two are one decision: the same
    comparison produces a warning on an archive with vectors in it and a piece
    of guidance on one without, and splitting them across two vars invites a
    yellow alert over a sentence that says nothing is wrong.
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    """Empty when there is nothing to say, which is what the form tests."""

    color: str = "yellow"


NO_ADVICE = Advice()
"""Nothing to say — the form renders nothing rather than an empty alert."""


class EmbedderReading(BaseModel):
    """What one load found: the embedder in force, and what it has embedded.

    The configuration here is the *effective* one — the file and the
    environment with the stored row laid over them — because that is what the
    form edits. Showing the stored row alone would leave somebody editing four
    empty boxes on an installation whose ``config.yaml`` configures a model,
    and their first save would silently overwrite it with blanks.

    There is deliberately no field for the API key. The reading is built from
    ``api_key_is_set``, a boolean the database computed, and the ciphertext is
    never fetched, never decrypted and never in this process — so the key
    cannot reach a state var by being in the object the state vars are built
    from.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = ""
    model: str = ""
    dimension: int = 0
    base_url: str = ""

    api_key_stored: bool = False
    """Whether a key is stored. Never which one."""

    embedded: int = 0
    """Messages carrying a vector from the model in force."""

    coverage_known: bool = False
    """Whether :attr:`embedded` was actually read.

    Its own flag rather than ``embedded = -1`` or ``None``: the count comes off
    the graph, the graph can be down, and "nobody could count" has to produce a
    different warning from "the count is zero". Conflating them would tell a
    user with a fully embedded archive that a model change costs nothing.
    """

    api_key_in_force: bool = False
    """Whether a key reaches the embedder — from anywhere, not only from here.

    Separate from :attr:`api_key_stored`, and the distinction is the point.
    ``api_key_stored`` answers "is there one in this database", which is the
    only key the Clear button can forget; this one answers "will the embedding
    call carry a bearer token", which is what decides whether OpenAI answers
    401. An installation configuring ``app.semantic.api_key`` in
    ``config.yaml`` — a configuration ``docs/user/semantic-search.md``
    documents — has the second without the first, and was told "No key is
    stored" over a yellow alert promising a 401 that could not happen. The
    likely reaction, pasting the key into the form, put a second copy of the
    secret in the database which then *won* the merge, so the next rotation in
    ``config.yaml`` was silently ignored.

    Still only a boolean: this is ``config.api_key is not None`` on an object
    the reader already holds, and asking whether something is ``None`` reveals
    nothing about it.
    """

    index_dimension: int = 0
    """The length the live vector index will actually index, or zero for none.

    Read off the running graph rather than taken from the configuration,
    because the configuration is exactly what can be wrong here —
    ``indexing.verify`` and ``SemanticSearch._knn`` both say so in as many
    words, and the form was the one surface that trusted it.
    """

    index_known: bool = False
    """Whether :attr:`index_dimension` was actually read. See
    :attr:`coverage_known`, which exists for the same reason and fails the same
    way."""


def identity(provider: str, model: str) -> str:
    """How the form names one embedder in a sentence.

    Provider and model together, because neither alone identifies what produced
    a vector: two providers can serve a model of the same name, and one
    provider serves many models.
    """
    if not provider or provider == "none":
        return "no embedder"
    return f"{provider} / {model or NO_MODEL}"


def vector_advice(reading: EmbedderReading, *, provider: str, model: str) -> Advice:
    """What changing the embedder identity costs, in the case at hand.

    Three cases and three different sentences, which is the whole reason this
    is a function rather than a constant. An archive with vectors in it is
    about to lose them from every semantic search; an archive with none loses
    nothing and needs guidance instead; and an archive nobody could count has
    to be warned as though it were the first, because assuming the second is
    the assumption that turns a warning into silence exactly when it matters.

    Nothing is deleted in any of the three. Saying so is not reassurance for
    its own sake — a user who thinks a model change might destroy their mail
    will not change the model, and the setting exists to be changed.
    """
    if provider == reading.provider and model == reading.model:
        return NO_ADVICE
    was, now = identity(reading.provider, reading.model), identity(provider, model)
    after = (
        f"Nothing is deleted: the messages and every other analysis are "
        f"untouched. Rebuild them with {EMBED_REMEDY}."
    )
    if not reading.coverage_known:
        return Advice(
            text=(
                f"This changes the embedder from {was} to {now}. Any vector "
                f"already stored was produced by the old one and a semantic "
                f"search under the new one will not find those messages. How "
                f"many there are could not be read just now. {after}"
            )
        )
    if reading.embedded == 0:
        return Advice(
            text=(
                f"This changes the embedder from {was} to {now}. No message is "
                f"embedded yet, so nothing is invalidated — but nothing is "
                f"searchable semantically either until the vectors are built: "
                f"use {EMBED_REMEDY}."
            ),
            color="blue",
        )
    return Advice(
        text=(
            f"This changes the embedder from {was} to {now}, and "
            f"{reading.embedded} message(s) are already embedded with the old "
            f"one. A semantic search under the new embedder will not find any "
            f"of them, and the page will not say so — it will simply return "
            f"fewer results. {after}"
        )
    )


INDEX_REMEDY = (
    "An index at another length is a new graph_migrations revision with a "
    "different DIMENSION plus `task graph:upgrade` — there is no button for it "
    "here, and on the desktop bundle there is no terminal for it either."
)
"""How an index of another length is actually made, said rather than implied.

The warning below used to state the cost — "the graph needs an index at 1536
before an embed job writes anything. Saving here does not create one." — and
name no way to get one. A cost with no remedy beside it reads as a refusal the
reader is expected to work around, and the one thing they must not do is save
and assume it worked. :data:`EMBED_REMEDY` sets the precedent: a warning names
the thing its reader can go and do.
"""


def index_advice(reading: EmbedderReading, *, dimension: int) -> Advice:
    """What the typed vector length costs, measured against the **live** index.

    Its own sentence beside :func:`vector_advice` because the remedy is a
    different one. A changed model is fixed by running a job; a changed
    dimension needs a vector index at the new length *first*, and until that
    exists the job's writes are accepted and dropped on the floor. Measured on
    the vendored FalkorDB: a vector of the wrong length leaves ``numDocuments``
    unchanged and ``indexingFailures`` at zero, so there is no error anywhere
    to notice.

    Against :attr:`EmbedderReading.index_dimension` and not against
    :attr:`EmbedderReading.dimension`, which is the correction a review asked
    for and which changes the answer in both directions. Comparing the typed
    value against the *configured* one meant that once 1536 had been saved over
    a 768 index the form compared 1536 with 1536 and said nothing — silence in
    exactly the state the page exists to warn about — while an administrator
    typing 768 to put it right was shown a red warning claiming the graph
    needed an index it already had. ``indexing.verify`` and
    ``SemanticSearch._knn`` both read the live index for this reason, in their
    own words because the configuration is what can be wrong.

    An unreadable index falls back to the old comparison rather than to
    silence. A settings page has to work on an installation whose graph is not
    running — configuring an embedder is something you do before it works — and
    a missing reading has to produce a weaker warning, never no warning.
    """
    if dimension <= 0:
        if dimension == reading.dimension:
            # Nothing configured and nothing typed — an archive that has never
            # had an embedder, not somebody emptying the box. `can_save` is
            # already false, and a red alert over an untouched form is noise.
            return NO_ADVICE
        return Advice(
            text=(
                "A vector needs a length of at least one float. Put the number "
                "back before saving — a zero-length embedder can never write a "
                "vector the graph will accept."
            ),
            color="red",
        )
    if not reading.index_known:
        if dimension == reading.dimension:
            return NO_ADVICE
        return Advice(
            text=(
                f"This changes the vector length from {reading.dimension} to "
                f"{dimension}, and the graph's own index could not be read just "
                f"now, so what it actually holds is unknown. A vector whose "
                f"length the index does not carry is accepted and stored and "
                f"silently not indexed — no error is raised anywhere. "
                f"{INDEX_REMEDY}"
            ),
            color="red",
        )
    if dimension == reading.index_dimension:
        return NO_ADVICE
    return Advice(
        text=(
            f"The graph's vector index holds {reading.index_dimension}-float "
            f"vectors and this embedder would write {dimension}. A vector of a "
            f"different length is accepted and stored but silently not indexed "
            f"— no error is raised anywhere — so no search would ever find one. "
            f"Saving here does not create an index. {INDEX_REMEDY}"
        ),
        color="red",
    )


def key_status(reading: EmbedderReading) -> str:
    """What the form says about the key, which is never its value.

    Three cases and three sentences, because there are three states and the
    old two-way answer collapsed the middle one into the wrong end of it. A key
    can be stored *here*, where the Clear button can forget it; it can come
    from the configuration file or the environment, where this page cannot
    touch it but the embedder still gets one; or there can be none anywhere,
    which is the only case that costs a 401.

    Saying which of the three is also what makes the Clear button's consequence
    readable — :data:`KEY_CLEARED` already promises that the file's key takes
    over afterwards, and a page asserting both that and "No key is stored" was
    contradicting itself.
    """
    if reading.api_key_stored:
        return "A key is stored here. Leave the box empty to keep it."
    if reading.api_key_in_force:
        return (
            "A key comes from the configuration file or the environment. "
            "Saving one here would override it."
        )
    return "No key is stored."


def host_advice(
    reading: EmbedderReading, *, provider: str, base_url: str, keyed: bool
) -> Advice:
    """What moving the embedding API to another host costs — including the key.

    The third warning, and the one that was missing. ``base_url`` is the only
    settable value that can both change what produces the vectors *and* send a
    credential somewhere new, and the form said nothing about it while being
    verbose about the two changes whose worst outcome is a re-embed.

    Two costs, and which of them applies is what ``keyed`` decides.

    The first is silent corruption of the vector space. Two hosts serving a
    model of the same name are not the same embedder — a different build or
    quantisation answers in another space — and nothing downstream can tell:
    the vectors land in the same index under the same ``embedding_model``, so
    the KNN filter matches both and ranks across them, which is the failure
    ``SEMANTIC_NEIGHBOURS``'s docstring records as measured.
    :func:`vector_advice` cannot see it, because provider and model are
    unchanged. Only the person typing knows, so this says so rather than
    deciding for them.

    The second is the credential. ``OpenAIEmbedder._headers`` attaches the
    stored bearer token to every call at whatever URL is configured, and the
    key does not have to be re-typed for the host to change — so nothing else
    on the page signals that the secret now travels somewhere else. Cleartext
    to anything but this machine gets the loudest colour on the page, because
    that is a token on a wire.

    Empty is not a host: ``SemanticConfig`` reads ``""`` as the provider's own
    endpoint, which is where an unconfigured archive already sends it.
    """
    if base_url.strip() == reading.base_url:
        return NO_ADVICE
    now = base_url.strip()
    where = f"`{now}`" if now else "the provider's own endpoint"
    space = (
        f"This points the embedder at {where}. A host serving a model of the "
        f"same name can still answer in a different vector space, and nothing "
        f"downstream can tell — the vectors are stored under the same model "
        f"name, so a search would rank across both. If it is the same service "
        f"moved, there is nothing to do; if it is a different one, rebuild the "
        f"vectors afterwards."
    )
    if not (keyed and provider == "openai" and now):
        return Advice(text=space, color="blue")
    if _is_cleartext_to_elsewhere(now):
        return Advice(
            text=(
                f"{space} It also sends this archive's stored API key to that "
                f"host on every call, and `{now}` is not encrypted — the token "
                f"would cross the network in the clear. The key is not "
                f"re-typed for this, so nothing else here says it travels."
            ),
            color="red",
        )
    return Advice(
        text=(
            f"{space} It also sends this archive's stored API key to that host "
            f"on every call. The key is not re-typed for this, so nothing else "
            f"here says it travels."
        )
    )


_LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")
"""Hosts that never put a packet on a network, so cleartext to them is not a leak."""


def _is_cleartext_to_elsewhere(url: str) -> bool:
    """Whether *url* would carry a bearer token unencrypted off this machine."""
    if not url.lower().startswith("http://"):
        return False
    host = url[len("http://") :].split("/", 1)[0].rsplit(":", 1)[0]
    return host.lower() not in _LOOPBACK


class EmbedJobView(BaseModel):
    """The embed job as the control shows it.

    The sibling of :class:`~mailarc_ui.insights.model.RebuildJobView`, and a
    separate class rather than a shared one because the two count different
    things. A ``derive`` job counts *stages* — the worker moves the row on once
    per analysis — while an embed run knows up front how many messages owe it a
    vector and reports messages written, per batch. Rendering one with the
    other's wording would print ``3 of 7 stages`` over a message count, which is
    not a cosmetic error: it would tell somebody watching a 40 000-message
    archive that seven units of work exist.

    Frozen, like every projection: a reading of the row, never a handle on it.
    """

    model_config = ConfigDict(frozen=True)

    job_id: int = 0
    status: str = ""
    status_color: str = "gray"
    percent: float = 0.0
    percent_label: str = _NO_PERCENT
    messages_label: str = ""
    error: str = ""
    active: bool = False
    cancel_requested: bool = False

    @classmethod
    def from_job(cls, job: SyncJob) -> EmbedJobView:
        """One job row, as the panel prints it.

        ``failed`` is folded into the label rather than dropped, because an
        embed run's failures are the number that explains a finished job whose
        coverage still is not complete: a batch the provider refused
        permanently leaves messages with no vector and no further job to fix
        them until somebody looks.
        """
        percent = percent_of(job.progress)
        total = job.progress.total
        counted = f"{job.progress.done} of {total} messages" if total > 0 else ""
        if counted and job.progress.failed:
            counted = f"{counted} · {job.progress.failed} refused"
        return cls(
            job_id=job.id,
            status=str(job.state),
            status_color=_STATE_COLORS.get(job.state, "gray"),
            percent=percent,
            percent_label=_NO_PERCENT if total <= 0 else f"{percent:.0f}%",
            messages_label=counted,
            error=job.error or "",
            active=job.state in (JobState.QUEUED, JobState.RUNNING),
            cancel_requested=job.cancel_requested,
        )


NO_EMBED_JOB = EmbedJobView()
"""No job is being followed. A sentinel keeps ``None`` out of the component."""


EMBED_NOT_ALLOWED = "Only an administrator may rebuild the vectors."
"""What the rebuild control says to a caller the gate refused.

Its own sentence beside :data:`NOT_ALLOWED` because it answers a different
button, and a form that refused a save by explaining a rebuild would send the
reader looking in the wrong place. Neither says more than the page's title.
"""

NO_EMBEDDER_TO_RUN = (
    "There is no embedder to run. Choose a provider above and save it first — "
    "an embed job under 'none' has nothing to compute a vector with and would "
    "fail as soon as a worker picked it up."
)
"""Why the button is dead on a default installation.

Refusing here rather than letting the job fail is the difference between a
sentence naming the next step and a red row in a job list saying the archive is
not set up for this. The job would raise ``SemanticUnavailable`` with a good
message; it would simply arrive minutes later, somewhere the reader is not
looking.
"""

EMBED_RUNNING = "A vector rebuild is already running."

EMBED_CANCEL_ASKED = (
    "Cancellation requested — the worker will stop after the current batch."
)
"""What a cancel says about a run a worker holds.

``request_cancel`` sets a flag and nothing else for a claimed job; the worker
reads it between batches. So the control goes on showing an active run with
both buttons disabled, and saying nothing would leave two dead buttons and no
reason for either. Everything already written stays written — an embed run is
resumable by construction, because it only ever looks for messages that have no
vector under the model in force.
"""

EMBED_CANCEL_TOOK_EFFECT = "That rebuild was cancelled before any worker picked it up."
"""And what it says about one nothing had claimed.

:meth:`~mailarc_sync.jobs.queue.JobQueue.request_cancel` ends an unclaimed job
outright rather than flagging it, so the button is live again and the message
has to say why instead of promising a worker that never came.
"""

UNSAVED_BEFORE_EMBED = (
    "The form holds changes that have not been saved. A rebuild embeds with the "
    "settings that are stored, not with what is on screen — save first, or the "
    "vectors are computed with the embedder you are in the middle of replacing."
)
"""The trap this control creates and has to close.

Queueing is one click away from four editable boxes, and the worker reads the
*stored* configuration. Somebody who changes the model because of the warning
above and then presses the button directly under it would otherwise re-embed
the whole archive with the model they were replacing, and every sentence on the
page would have been telling the truth while they did it.
"""


def gave_up_on(job: EmbedJobView) -> str:
    """Why the page stopped following, in the words the situation deserves."""
    if job.status == "queued":
        return (
            "Not following this rebuild any more — no worker has picked it up. "
            "Start one with `task sync:worker`, then reload. Nothing is lost: "
            "the job is still queued."
        )
    return (
        "Not following this rebuild any more — it has been going for a while. "
        "Reload to see where it got to."
    )


PROVIDER_OPTIONS = [
    {"value": SemanticProvider.NONE.value, "label": "None — no embedder"},
    {"value": SemanticProvider.OLLAMA.value, "label": "Ollama — a local model server"},
    {
        "value": SemanticProvider.OPENAI.value,
        "label": "OpenAI — uploads message text to a third party",
    },
]
"""The three providers, labelled by what choosing one means rather than by name.

``openai`` is the one where the label has to carry a consequence: every message
body embedded that way is sent, once, to somebody else's service. A user
picking from a list of three words has no way to know that, and a mail archive
is the last place to leave it implicit.
"""

NOT_ALLOWED = "Only an administrator may change the embedder."
"""What the form says to a caller the gate refused.

A sentence rather than an empty form, for the reason the search panel says
something: a page of blank boxes is indistinguishable from an archive that has
nothing configured, and the difference matters here. It gives nothing away that
the page's own title does not.
"""

NO_CONTROL = (
    "The embedder settings are not wired up in this build — nothing published a "
    "SemanticControl. The composition root does; did app.composition run?"
)
"""The developer's error, in the one place a user could meet it.

Its own sentence and not a bare ``KeyError``, because a half-wired application
and a broken one look identical from a form and have completely different
fixes.
"""

SAVE_FAILED = (
    "The settings could not be saved. The archive's database did not accept the "
    "write; nothing was changed. The details are in the application log."
)
"""A fault, said without quoting the driver.

A SQLAlchemy message can carry a filesystem path out of this installation, and
this string is rendered into a browser — the same choice the search panel makes
for the same reason. Nothing is lost: the exception is logged with its
traceback first.
"""

SETTINGS_MOVED = (
    "Somebody else changed the embedder settings while this form was open, so "
    "nothing was written — saving would have put their change back without "
    "either of you being told. What is below is what is stored now; make your "
    "change again over it."
)
"""Two administrators, one row, and a refusal rather than a lost update.

Every save overwrites all four columns, so the second person to press Save
silently undoes the first — and *which* settings these are is what makes that
worse than the usual lost update: putting a dimension back undoes the change the
vector index was migrated for, after which the archive embeds at a length the
index does not carry, accepted and never indexed. The stale form compares
against its own stale baseline, so it does not warn either.

The form is re-read before this is shown, so the sentence's last clause is true
by the time anybody reads it.
"""

SAVED_NOT_SHOWN = (
    "Saved, but the settings could not be read back, so what is below may be "
    "out of date. Reload the page. The details are in the application log."
)
"""The write landed and the re-read did not — which is neither of the other two.

:data:`LOAD_FAILED` would be a lie here ("nothing was changed" — something
was), and :data:`SAVED` would be a worse one: it would put the values the form
held *before* the save under a green notice, which is the one screen that makes
somebody save a second time.
"""

LOAD_FAILED = (
    "The embedder settings could not be read. Nothing was changed. The details "
    "are in the application log."
)
"""The same policy on the way in, which the load path did not have.

:data:`SAVE_FAILED` refuses to quote the driver and the load path one method
away rendered ``str(error)`` straight into the browser. The case is real rather
than hypothetical — ``semantic_settings`` arrives in a migration a checkout may
not have applied, which ``semantic_settings_lifespan`` names — and what
SQLAlchemy says then is the message plus ``[SQL: SELECT
semantic_settings.api_key IS NOT NULL ...]`` plus ``[parameters: (1,)]``, or,
for a SQLite archive opened by path, ``unable to open database file`` with the
store's location in it.

The one exception still shown verbatim is :data:`NO_CONTROL`, which this
application raises itself and which says something no generic sentence can:
that the build is half-wired rather than broken.
"""

KEY_NOT_STORED = (
    "The API key was not stored, and nothing else was written either — the "
    "whole save was rolled back. This archive's encryption key is not usable, "
    "so a secret cannot be encrypted at rest. The details are in the "
    "application log."
)
"""The one failure that must never be reported by quoting the exception.

``StatementError`` renders its bind parameters, and the bind parameter of this
write is the plaintext key.
:class:`~mailarc_core.database.repositories.ApiKeyNotStored` exists to carry the
cause without them, so what reaches the log is the stripped message and what
reaches the browser is this. Both halves of the save are one transaction, which
is why this can promise that nothing was written.
"""

SAVED = "Saved, and the running application has adopted it."

SAVED_NOT_ADOPTED = (
    "Saved, but the running application could not adopt the change — it is "
    "still using the previous embedder. Restart the application."
)
"""Two different outcomes, because the second one is a lie if it is not said.

The write and the adoption are separate steps and the second can fail on its
own — the embedder's host may be unreachable at the moment it is rebuilt. A
form that said "Saved" either way would leave somebody searching against the
old model and blaming the model.
"""

KEY_CLEARED = (
    "The stored API key was removed. The archive now uses whatever key the "
    "configuration file or the environment supplies, and OpenAI without one "
    "answers 401."
)

RESET = (
    "These settings were forgotten. The archive is back to what the "
    "configuration file and the environment say."
)

WORKER_NOTE = (
    "The import worker reads these settings once, when it starts, and the embed "
    "job runs there. A change reaches the pages immediately and the worker at "
    "its next start."
)
"""The one thing about this form that is not true of the page in front of you.

Stated because the gap is invisible and its symptom is baffling: a search that
uses the new model while the job filling its index still uses the old one. On
the desktop the worker is the application's own child, so restarting the
application is enough; under Docker or systemd it is its own unit.
"""


REINDEX_FAILED = (
    "The vector index could not be rebuilt. The archive is unchanged; the log "
    "has the reason."
)
"""What a failed rebuild says. It names the archive as unchanged on purpose.

The operation drops the index before it builds the new one, so a reader who is
told only that it failed cannot know whether their vectors survived. They did:
the messages are untouched either way, and the worst case is a graph with no
index, which every semantic path already refuses loudly rather than answering
wrongly.
"""


def reindexed(cleared: int) -> str:
    """What a finished rebuild says, including the bill it just ran up.

    The count is the point. A resize forgets every stored vector — one of the
    old length in an index of the new one is accepted, never indexed and found
    by no search — so the honest report is not "done" but "done, and this many
    messages now need embedding again".
    """
    if cleared == 0:
        return "The vector index was rebuilt. Nothing had been embedded yet."
    return (
        f"The vector index was rebuilt and {cleared} stored vector(s) were "
        "forgotten. Run Rebuild the vectors to compute them again."
    )
