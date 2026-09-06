"""A question in German, a filled-in search form out — and nothing in between.

The third way to ask this archive something, beside full text and vectors, and
the only one that reads a *sentence*. "Suche alle Nachrichten von meiner
Hausverwaltung Santak" is a filled search form written as prose: a sender, and
"Santak" is a display name rather than an address. This package turns the one
into the other.

What it deliberately does **not** do is answer. There is no summarisation, no
prose reply, no second call over message bodies — the output is a
:class:`~mailarc_core.archive.search.SearchFilters` in all but name, and the
search page writes it into the visible form fields before running the ordinary
search. That is what keeps the feature honest: an interpretation the reader can
see is one they can correct, and a wrong reading costs a click rather than a
wrong answer delivered with confidence.

**No archive data is sent to the model.** One call, whose whole input is the
question, the date and a static schema. Grounding happens afterwards and
locally: :mod:`~mailarc_analytics.nlq.resolve` matches the words that came back
against the archive's own addresses, file families and mailboxes. Handing the
model a candidate list would have been simpler and would have meant uploading a
list of who the user knows. Semantic search makes the opposite trade — it
uploads message bodies once and gets meaning for it — and both are defensible
as long as a reader can tell which one they turned on.

Modules, one job each:

``config``
    :class:`~mailarc_analytics.nlq.config.LlmConfig`, whose ``provider``
    defaults to ``none``. Its own block rather than a field on
    :class:`~mailarc_analytics.semantic.config.SemanticConfig`: the two models
    differ in what leaves the machine, and an installation may reasonably want
    either without the other.
``ports``
    :class:`~mailarc_analytics.nlq.ports.ChatPort`. The third seam that clears
    the "a port needs a second implementation" bar — three exist on day one.
``chat``
    The three adapters, :func:`~mailarc_analytics.nlq.chat.build_chat_client`
    (``None`` when nothing is configured, never a null object), and
    :func:`~mailarc_analytics.nlq.chat.intent_json`, which reads an object out
    of whatever a model actually sent.
``prompt``
    The instruction. Constant except for the date — see the module docstring,
    which is where the privacy argument above is enforced rather than restated.
``resolve``
    Pure. Mentions to addresses, words to file families, a named period to two
    dates. The German cases live and die here and none of them needs a model.
``interpreter``
    :class:`~mailarc_analytics.nlq.interpreter.QueryInterpreter` — the sequence,
    and the error boundary that keeps ``mailarc-core``'s mail taxonomy from
    reaching a search page that would show a wrong API key as a fault.

``model`` and ``errors`` hold the values and the messages the rest share.

The rule that binds all of them, and it is
:mod:`mailarc_analytics.semantic.errors`' rule unchanged: **nothing here
returns an empty filter to mean "not available"**. An empty
:class:`~mailarc_core.archive.search.SearchFilters` is not "nothing matched" —
it is the browse-the-whole-archive read, so a swallowed failure would answer a
narrow question with every message in the archive and look like it worked.
"""

from mailarc_analytics.nlq.chat import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    AzureOpenAIChat,
    OllamaChat,
    OpenAIChat,
    build_chat_client,
    intent_json,
)
from mailarc_analytics.nlq.config import (
    LlmConfig,
    LlmControl,
    LlmOverrides,
    LlmProvider,
)
from mailarc_analytics.nlq.errors import (
    CONFIGURE_LLM,
    NO_LLM,
    SETTINGS_PAGE,
    NlqError,
    NlqRefused,
    NlqUnavailable,
)
from mailarc_analytics.nlq.interpreter import QueryInterpreter
from mailarc_analytics.nlq.model import (
    AccountOption,
    DirectoryEntry,
    FamilyOption,
    Order,
    Period,
    QueryIntent,
    ResolvedQuery,
    Vocabulary,
    strict_schema,
)
from mailarc_analytics.nlq.ports import ChatPort
from mailarc_analytics.nlq.prompt import system_prompt
from mailarc_analytics.nlq.resolve import (
    resolve,
    resolve_account,
    resolve_addresses,
    resolve_families,
    window_of,
)

__all__ = [
    "CONFIGURE_LLM",
    "DEFAULT_BASE_URLS",
    "DEFAULT_MODELS",
    "NO_LLM",
    "SETTINGS_PAGE",
    "AccountOption",
    "AzureOpenAIChat",
    "ChatPort",
    "DirectoryEntry",
    "FamilyOption",
    "LlmConfig",
    "LlmControl",
    "LlmOverrides",
    "LlmProvider",
    "NlqError",
    "NlqRefused",
    "NlqUnavailable",
    "OllamaChat",
    "OpenAIChat",
    "Order",
    "Period",
    "QueryIntent",
    "QueryInterpreter",
    "ResolvedQuery",
    "Vocabulary",
    "build_chat_client",
    "intent_json",
    "resolve",
    "resolve_account",
    "resolve_addresses",
    "resolve_families",
    "strict_schema",
    "system_prompt",
    "window_of",
]
