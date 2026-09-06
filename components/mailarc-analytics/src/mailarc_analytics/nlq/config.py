"""Which model reads a question, and where it lives.

:attr:`LlmConfig.provider` defaults to ``none``, for the reason
:class:`~mailarc_analytics.semantic.config.SemanticConfig`'s does: §7.4 keeps
the desktop application free of prerequisites, and an archive with no
interpreter is a complete installation rather than a broken one — full-text
search is untouched and the **Natural** segment says what to configure.

**This is deliberately not a field on ``SemanticConfig``.** The two models are
not variations of each other. An embedder is run once over the whole archive
and its name is written onto every node it touches; an interpreter is run once
per question and stores nothing. More to the point, they differ in what leaves
the machine: embedding uploads message bodies, interpreting uploads the
sentence a person typed and nothing else. An installation may reasonably want
either one without the other, and a single ``provider`` could not express that.

The five settings a person can answer — provider, model, base URL, key — are
the same five ``/admin/embedder`` offers for the embedder, and the rest is
calibration that stays in the file for the reason
:class:`~mailarc_analytics.semantic.config.SemanticOverrides` gives: a form
offering ``max_output_tokens`` invites a change to the one number keeping the
answer parseable.

There is no ``port``. ``base_url`` is how this repository spells an endpoint
(:class:`~mailarc_analytics.semantic.config.SemanticConfig` has only that one
too), and a port beside it would be a second source of truth for one value —
they would disagree the first time somebody put Ollama behind a proxy.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum

from appkit_commons.configuration.base import BaseConfig
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import SettingsConfigDict


class LlmProvider(StrEnum):
    """Where an interpretation comes from, if it comes from anywhere."""

    NONE = "none"
    """No interpreter. The default, and a complete state rather than a broken
    one: full-text and semantic search are untouched, and the **Natural**
    segment is offered disabled with the setting to change written beside it.
    """

    OLLAMA = "ollama"
    """A local model server. No account, no key, and — since the question is
    the only thing sent — nothing about this archive leaves the machine at
    all."""

    OPENAI = "openai"
    """OpenAI's chat completions endpoint. Needs a key. What is uploaded is
    the sentence the user typed, never a message body."""

    AZURE_OPENAI = "azure_openai"
    """A deployment in your own Azure OpenAI resource. Needs a key, an
    endpoint and a deployment name — none of which can be guessed, so all
    three are refused by name when unset rather than sent nowhere."""


class LlmConfig(BaseConfig):
    """The model that turns a question into a filter, and how it is reached."""

    model_config = SettingsConfigDict(
        env_prefix="app_llm_",
        env_file=".env",
        populate_by_name=True,
    )

    provider: LlmProvider = LlmProvider.NONE
    """Which interpreter to build, or none at all.

    ``none`` is the supported default, not a disabled feature. Every surface
    that needs one says so in a sentence naming this setting; none of them
    answers with an empty result list, because an empty list is a valid answer
    to a search and reads as "your archive holds nothing about that".
    """

    model: str = ""
    """The model's name; empty means the provider's own default.

    Unlike the embedder's, this name is stored nowhere and can be changed
    freely — nothing in the graph was produced by it, so a change costs the
    next question and not a rebuild. For :attr:`LlmProvider.AZURE_OPENAI` it is
    the **deployment** name, and empty is not a usable default there: a
    deployment is unique to one customer's resource.
    """

    base_url: str = ""
    """The chat API's root; empty means the provider's own default.

    A setting rather than a constant for the reason
    :attr:`~mailarc_analytics.semantic.config.SemanticConfig.base_url` is one:
    it is what lets every adapter test run against a local HTTP server, so no
    test in this repository ever talks to Ollama or OpenAI.

    Azure has no default to fall back to, and only its **v1** root works here:
    ``https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1``. The legacy
    surface puts the deployment in the path and the API version in a query
    string, and the shared ``_post`` sends neither.
    """

    api_key: SecretStr | None = None
    """The credential, for the providers that want one.

    Ollama ignores it. OpenAI and Azure OpenAI without it both answer 401,
    which the adapter reports as an auth failure rather than as something to
    retry — Azure's key travels in an ``api-key`` header rather than OpenAI's
    ``Authorization: Bearer``, which is the one thing
    :class:`~mailarc_analytics.nlq.chat.AzureOpenAIChat` changes about how this
    value is sent.
    """

    request_timeout: float = 60.0
    """Seconds one interpretation may take before it counts as transient.

    Half the embedder's two minutes, and the difference is who is waiting: an
    embed job runs in a worker and can afford a cold model, while this one runs
    between a person pressing **Search** and the list appearing. A minute is
    already past the point where the answer is useful.
    """

    max_output_tokens: int = 512
    """How long an answer may be before it is cut off.

    The answer is one flat JSON object, so this is generous — but it is a
    *hard* limit rather than a hint, and an answer cut off mid-object is
    truncated JSON. Both OpenAI-shaped providers report that as
    ``finish_reason: "length"``, which the adapter checks before parsing so the
    failure names this setting instead of arriving as a parse error.
    """

    temperature: float | None = 0.0
    """How much the model may vary; ``None`` omits the parameter entirely.

    Zero, because the same question should produce the same filter — a search
    that quietly returns a different set each time it is asked is worse than
    one that is merely wrong. ``None`` is not "the default": reasoning models
    accept only their own temperature and answer 400 to any other, so the key
    has to be absent rather than set to something they tolerate.
    """

    max_question_chars: int = 500
    """How much of a question reaches the prompt.

    The cut that stops a pasted mail thread from becoming the prompt. It is far
    past any question a person types and far short of anything that would cost
    real tokens.
    """

    max_resolved_addresses: int = 5
    """How many archived addresses one name in the question may stand for.

    "Telekom" legitimately matches several senders, and filling all of them in
    is the useful reading — :attr:`~mailarc_core.archive.search.SearchFilters.
    senders` means *any of these*. But a bare surname can match dozens, and a
    **From** picker that silently filled itself with dozens of addresses would
    be a filter nobody could read. Past this many the mention counts as
    unresolved and is reported as such.
    """


class LlmOverrides(BaseModel):
    """What somebody may change about the interpreter without editing a file.

    Four of :class:`LlmConfig`'s nine fields —
    :class:`~mailarc_analytics.semantic.config.SemanticOverrides`' cut, made
    the same way and for the same reason. These are the ones a person can
    answer from what they have: which service, which model, where it lives, and
    the key that opens it. The rest are calibration, and a form offering
    ``max_output_tokens`` invites a change to the one number keeping an answer
    parseable.

    **``None`` means "not set", and that is the whole precedence rule.**
    :meth:`applied_to` lays only what is present over the configured value, so
    the file and the environment go on answering for everything nobody decided
    — and an installation that has stored nothing resolves to precisely the
    configuration it had before this class existed. An empty *string* is a
    decision rather than an absence: ``model`` and ``base_url`` already read
    ``""`` as "whatever this provider's own default is", so clearing a field
    needs no second sentinel.

    There is no ``dimension`` twin here to guard, because nothing this model
    produces is stored — which is the whole difference between changing an
    interpreter and changing an embedder.
    """

    model_config = ConfigDict(frozen=True)

    provider: LlmProvider | None = None
    """Which interpreter to build, or ``None`` to leave that to the file."""

    model: str | None = None
    """The model's name; ``""`` is the provider's own default."""

    base_url: str | None = None
    """The chat API's root; ``""`` is the provider's own default."""

    api_key: SecretStr | None = None
    """The credential. Stored encrypted, and never read back to a browser."""

    def applied_to(self, config: LlmConfig) -> LlmConfig:
        """Return *config* with every value this carries laid over it.

        ``model_copy`` rather than constructing the settings class again, for
        the reason :meth:`~mailarc_analytics.semantic.config.SemanticOverrides.
        applied_to` gives: ``BaseSettings.__init__`` re-runs its own sources, so
        a round trip through it would re-read ``.env`` and the environment on
        the way past and could hand back a different object than went in.

        Returns *config* itself when there is nothing to lay over it, so
        "nothing is stored" is identity rather than an equal copy — which is
        what lets the composition root skip rebuilding a client it has no
        reason to rebuild.
        """
        stored = {name: value for name, value in self if value is not None}
        if not stored:
            return config
        return config.model_copy(update=stored)


class LlmControl(BaseModel):
    """The two verbs only the composition root can perform on this setting.

    :class:`~mailarc_analytics.semantic.config.SemanticControl` minus its third.
    A page that edits the interpreter needs to know what the archive is running
    with right now — file, environment and stored row, merged — and to be able
    to say "read the store again and rebuild what came out of it", which means
    clearing a cached client and closing its connection pool. Both are §4.1's
    composition root and nobody else's.

    There is no ``reindex``. That verb exists on the embedder's control because
    a vector index has one fixed length and changing the model can leave every
    stored vector unfindable. Nothing here writes anything, so changing this
    model costs the next question and there is nothing to rebuild — which is
    also why this form needs no warning before a save.
    """

    model_config = ConfigDict(frozen=True)

    current: Callable[[], LlmConfig]
    """The configuration in force as of now — stored over file over default.

    A callable rather than the value, because the value changes underneath a
    long-lived registry entry every time somebody saves, and a page holding
    what it was given at startup would go on reporting the model it replaced.
    """

    reload: Callable[[], Awaitable[LlmConfig]]
    """Read the store again and adopt the result, returning what is now in force.

    Idempotent, and a no-op when nothing changed — so a page may call it after
    every save without having to work out whether the save changed anything.
    """
