"""What a vector costs, where it comes from, and how strict the search is.

One setting decides everything else here: :attr:`SemanticConfig.provider`
defaults to ``none``, and that is the whole reason the desktop application has
no prerequisites. Without an embedder A1-A3 still run in full — co-recipients,
topics and templates are Cypher and SimHash — and exactly two things are
missing: semantic search, and A2's sixth signal. Defaulting to ``ollama``
instead would make a mail archive refuse to analyse anything until the user had
installed a model server, for a feature most archives never need.

``dimension`` is the one number that is not free to change. The FalkorDB vector
index is migrated to a fixed dimension, and a vector of any other length is
accepted, stored and **silently not indexed** — measured: writing a length-2
vector into a dimension-4 index leaves ``numDocuments`` unchanged and
``indexingFailures`` at zero. So a mismatch does not fail, it disappears, which
is why :func:`~mailarc_analytics.semantic.indexing.verify` compares this number
against the live index before a job writes anything.

``base_url`` and ``model`` are deliberately empty by default and resolved per
provider in :func:`~mailarc_analytics.semantic.embedder.build_embedder`. One
shared default cannot be right for both: ``http://localhost:11434`` pointed at
OpenAI is a connection error, and ``nomic-embed-text`` sent to OpenAI is a 404
for a model that does not exist there. An empty value means "whatever this
provider's default is"; anything else is the user's word and is used as it
stands — which is also what lets a test point the whole adapter at a local
``pytest-httpserver``, the same way ``GmailConfig`` carries its two URLs.

:class:`SemanticOverrides` is the same five of those settings a *person* may
change while the archive is running, each of them optional. It is here rather
than next to whatever stores it because the precedence rule — an unset override
falls through to the file — is a statement about these defaults, and a reader
asking "what wins" should find the answer beside the thing that loses. Nothing
in this package reads it: the composition root merges and hands the result
down, because a component that read a database would have to know that a
database exists.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum

from appkit_commons.configuration.base import BaseConfig
from pydantic import BaseModel, ConfigDict, PositiveInt, SecretStr
from pydantic_settings import SettingsConfigDict


class SemanticProvider(StrEnum):
    """Where embeddings come from, if they come from anywhere."""

    NONE = "none"
    """No embedder. The default, and a complete state rather than a broken one.

    Everything deterministic keeps working; semantic search says so instead of
    answering, because an empty result would read as "nothing matched".
    """

    OLLAMA = "ollama"
    """A local model server. No account, no key, no data leaves the machine."""

    OPENAI = "openai"
    """OpenAI's embeddings endpoint. Needs a key, and sends message text to it.

    Worth stating plainly in any UI that offers it: every body embedded this
    way is uploaded, once, to a third party.
    """


class SemanticConfig(BaseConfig):
    """The embedder, the vectors it produces, and what a search does with them."""

    model_config = SettingsConfigDict(
        env_prefix="app_semantic_",
        env_file=".env",
        populate_by_name=True,
    )

    provider: SemanticProvider = SemanticProvider.NONE
    """Which embedder to build, or none at all.

    ``none`` is not a disabled feature, it is the supported default: §7.4 keeps
    the desktop app free of prerequisites, and every surface that needs a
    vector is required to say so in words rather than to return nothing.
    """

    model: str = ""
    """The embedding model's name; empty means the provider's own default.

    Written onto every node it embeds as ``Message.embedding_model``, which is
    what makes a later change of model *detectable* — the re-embedding read
    selects on ``m.embedding_model <> $model``, so switching costs one job and
    not a re-import. A model name that lies about which model produced a vector
    would leave those messages unfindable and unrecomputable at the same time.
    """

    dimension: int = 768
    """Floats per vector. **Must equal the graph migration's ``DIMENSION``.**

    768 rather than 1536 because the local path is the one that has to work
    without an account: ``nomic-embed-text`` is 768 natively *and takes no
    ``dimensions`` parameter*, so 768 is the only length it can answer, while
    ``text-embedding-3-small`` is 1536 natively and can be *asked* for 768
    through the API's ``dimensions`` parameter — so 768 is the only length both
    providers can produce, and raising it to 1536 would buy OpenAI nothing it
    does not already have while costing Ollama everything. It also halves what
    the index costs: measured on the vendored FalkorDB, 7.3 KB per message at
    768 against 14 KB at 1536, which is 0.73 GB against 1.4 GB for a hundred
    thousand messages.

    The number a *fresh* archive starts at, not a ceiling: an installation that
    wants OpenAI's native 1536 raises it on the settings page, which rebuilds
    the index at the new length rather than leaving the two disagreeing.
    """

    base_url: str = ""
    """The embedding API's root; empty means the provider's own default.

    A setting and not a constant for the reason ``GmailConfig.api_base_url`` is
    one: it is what lets every adapter test run against a local HTTP server, so
    no test in this repository ever talks to Ollama or OpenAI.
    """

    api_key: SecretStr | None = None
    """The bearer token, for the providers that want one.

    Ollama ignores it. OpenAI without it is a 401, which the adapter reports as
    an auth failure rather than as something to retry.
    """

    batch_size: int = 32
    """Texts per HTTP call.

    Deliberately the conservative number rather than the fast one. OpenAI is
    comfortable with 128 and more; a local model on a CPU is not, and a batch
    that takes longer than :attr:`request_timeout` fails the whole batch rather
    than slowing it down. One setting for both providers, tuned for the one
    that can choke.
    """

    page_size: int = 500
    """Messages one graph round trip claims for embedding.

    The unit of a job's progress and of its memory: one page of truncated
    bodies is resident at a time, roughly four megabytes at these defaults, and
    the progress bar moves once per page. Reading the whole archive first would
    be simpler and would hold a hundred thousand bodies in Python next to a
    FalkorDB running in the same process tree.
    """

    request_timeout: float = 120.0
    """Seconds one embedding call may take before it counts as transient.

    Two minutes, because a local model on a cold CPU really does take that long
    for a first batch. Short enough to notice a hung socket, long enough not to
    turn a slow machine into a failing one.
    """

    max_body_chars: int = 8_000
    """Characters of ``body_clean`` an embedding is computed over.

    Roughly two thousand tokens, under every provider's per-input limit, and
    past the point where more text helps: an embedding of a whole quoted thread
    describes the thread, not the message. Truncating here rather than relying
    on the provider's own truncation keeps the two providers comparable — one
    truncates by default, the other refuses the input.
    """

    knn_over_fetch: int = 10
    """How many neighbours are asked for per neighbour returned.

    FalkorDB's KNN cannot be filtered before the fact: a ``WHERE`` after
    ``db.idx.vector.queryNodes`` narrows the *k* rows already chosen, so asking
    for ten and filtering leaves nine. Asking for ``limit × this`` and cutting
    afterwards is the only shape that returns a full page, and it is also what
    lets the search skip messages that have no canonical id.
    """

    topic_similarity_min: float = 0.82
    """Cosine similarity below which two messages are not the same topic.

    Signal 6's gate, and the only thing keeping it honest. Its *weight* in the
    clustering is deliberately the weakest of the six, so an embedding edge can
    never outvote a shared ticket token — what stops it from joining half the
    archive is selectivity, not weight. 0.82 is high: at 0.7 a mail about an
    invoice and a mail about a delivery note are neighbours in every model.
    """

    topic_neighbours: int = 5
    """How many close messages signal 6 lets one message name.

    The other half of :attr:`topic_similarity_min`, and the cheaper of the two
    guards: the threshold decides *whether* a pair is offered, this decides how
    many can be. Five, because a KNN answers per message and the rebuild pays
    for every row — a hundred thousand messages at k=5 is half a million pairs
    offered into a weak-pair budget that defaults to two million, so the
    threshold is what does the real work and this only stops one message in a
    dense cluster from naming a hundred.
    """

    task_prefix: bool = False
    """Whether the Ollama adapter prefixes its input with a task instruction.

    ``nomic-embed-text`` is trained to be given ``search_document: `` when
    indexing and ``search_query: `` when searching, and the two produce
    *different* vectors for the same words — which is the point. What is not
    established is whether Ollama's packaged template already adds them; if it
    does, adding them again embeds the instruction twice.

    Off, therefore, until somebody embeds one text with and without the prefix
    and compares the vectors. Turning it on afterwards changes every vector the
    model produces, so it is a re-embedding job, not a toggle — which is
    exactly what ``Message.embedding_model`` cannot detect, since the model
    name does not change. Decide it once, before the first import.
    """


class SemanticOverrides(BaseModel):
    """What somebody may change about the embedder without editing a file.

    Five of :class:`SemanticConfig`'s thirteen fields, and the cut is not
    arbitrary: these are the ones a person can answer from what they have —
    which service, which model, how long a vector, where it lives, and the key
    that opens it. The other eight are calibration, and offering them would
    invite a change to ``topic_similarity_min`` from somebody who has no way to
    know it is the only thing keeping signal 6 out of half the archive.

    **``None`` means "not set", and that is the whole precedence rule.**
    :meth:`applied_to` lays only what is present over the configured value, so
    the file and the environment go on answering for everything nobody has
    decided — and an installation that has stored nothing resolves to precisely
    the configuration it had before this class existed. An empty *string*, by
    contrast, is a decision: ``model`` and ``base_url`` already read ``""`` as
    "whatever this provider's own default is", so clearing a field is
    expressible without a second sentinel.

    ``dimension`` is a :class:`~pydantic.PositiveInt` rather than an ``int``
    because a stored zero is not a smaller index, it is an embedder that can
    never write a vector the graph will accept. Catching it here turns a
    hand-edited database into one refused value the composition root logs,
    instead of a running archive whose embeddings silently go nowhere.
    """

    model_config = ConfigDict(frozen=True)

    provider: SemanticProvider | None = None
    """Which embedder to build, or ``None`` to leave that to the file."""

    model: str | None = None
    """The embedding model's name; ``""`` is the provider's own default."""

    dimension: PositiveInt | None = None
    """Floats per vector. Changing this invalidates every stored vector (§7.4)."""

    base_url: str | None = None
    """The embedding API's root; ``""`` is the provider's own default."""

    api_key: SecretStr | None = None
    """The bearer token. Stored encrypted, and never read back to a browser."""

    def applied_to(self, config: SemanticConfig) -> SemanticConfig:
        """Return *config* with every value this carries laid over it.

        ``model_copy`` rather than constructing the settings class again:
        ``BaseSettings.__init__`` re-runs its own sources, so a round trip
        through it would re-read ``.env`` and the environment on the way past
        and could hand back a different object than the one that went in. The
        fields here are ``SemanticConfig``'s own field types and pydantic has
        already validated them, so a second validation pass has nothing left to
        catch.

        Returns *config* itself when there is nothing to lay over it, so
        "nothing is stored" is identity rather than a copy that merely compares
        equal — which is what lets the composition root skip rebuilding an
        embedder it has no reason to rebuild.
        """
        stored = {name: value for name, value in self if value is not None}
        if not stored:
            return config
        return config.model_copy(update=stored)


class SemanticControl(BaseModel):
    """The two verbs only the composition root can perform on this settings
    object, in the one shape a component is allowed to be handed them in.

    A page that *edits* the embedder needs two things the store alone cannot
    answer. What is the archive running with **right now** — which is the file,
    the environment and whatever is stored, merged — and "read the store again
    and rebuild what came out of it", which means clearing a cached embedder
    and closing its connection pool. Both are §4.1's composition root and
    nobody else's: a component that did either would have to know that a
    database and a service registry exist.

    So the composition root builds one of these and registers it, exactly as it
    registers the analytics reader and the search, and the settings page reads
    it back out — ``mailarc-ui`` may not import ``app``, and the registry is
    keyed by type, so this class is the key. It holds no state of its own and
    is deliberately not a ``Protocol``: there is one implementation and one
    caller, and a port with one of each is indirection rather than architecture
    (§5).

    :attr:`current` hands back the whole :class:`SemanticConfig`, API key
    included. A caller in front of a browser reads the four settable fields off
    it and never :attr:`SemanticConfig.api_key`; whether a key is *stored* is
    ``SemanticSettingsRepository.api_key_is_set``, which answers with a boolean
    the database computed and never decrypts anything.
    """

    model_config = ConfigDict(frozen=True)

    current: Callable[[], SemanticConfig]
    """The configuration in force as of now — stored over file over default.

    A callable rather than the value, because the value changes underneath a
    long-lived registry entry every time somebody saves, and a page holding the
    object it was given at startup would go on reporting the embedder it
    replaced.
    """

    reload: Callable[[], Awaitable[SemanticConfig]]
    """Read the store again and adopt the result, returning what is now in force.

    Idempotent, and a no-op when nothing changed — so a page may call it after
    every save without having to work out whether the save changed anything.
    """

    reindex: Callable[[], Awaitable[int]]
    """Rebuild the vector index at the length now in force; answer how many
    vectors that forgot.

    The third verb, and the one that makes the length a real setting rather
    than a number the form is allowed to display. A vector index has one fixed
    length, fixed when it is built, so changing the embedder to a model of a
    different size leaves an index that quietly refuses every vector written
    against it — stored, unindexed, and found by no search.

    Not idempotent in the way :attr:`reload` is, and deliberately not hidden
    inside a save: it discards every stored vector, because one of the old
    length in an index of the new one is worse than none. What it returns is
    how many messages therefore need embedding again, which is the number a
    human should be shown before they are asked to confirm.
    """
