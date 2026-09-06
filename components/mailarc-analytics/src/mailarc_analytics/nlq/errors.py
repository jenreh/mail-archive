"""What natural-language search says when it cannot answer.

The rule :mod:`mailarc_analytics.semantic.errors` states at length holds here
unchanged: **an unanswerable question raises a sentence and never returns an
empty filter.** An empty :class:`~mailarc_core.archive.search.SearchFilters` is
not "nothing matched" — it is the browse-the-whole-archive read, so a swallowed
failure would answer a narrow question with every message in the archive and
look like it had worked.

Two classes, and the split is the same one that taxonomy makes: does a human
have to change a setting, or can the same caller sensibly ask something else?

Deliberately **not** :class:`~mailarc_core.mail.errors.MailError`, which the
adapters in :mod:`mailarc_analytics.nlq.chat` do raise. That taxonomy answers
"retry, re-consent, or skip", and those are the right three answers for an HTTP
call inside a job. They are the wrong three at the top of an interactive
search: :class:`~mailarc_core.mail.errors.MailAuthError` for a wrong API key is
a *configuration* state a person must fix, and letting it out of this package
would land it in the search page's bare ``except Exception`` and be shown as
"the archive could not be searched" — a fault, with no remedy, for a setting.
So :class:`~mailarc_analytics.nlq.interpreter.QueryInterpreter` catches all
three and re-raises as one of the two below.
"""


class NlqError(Exception):
    """Anything that stopped a question from becoming a filter.

    One base so that a page or a tool can turn the pair into a single readable
    failure with one ``except``, exactly as
    :class:`~mailarc_analytics.semantic.errors.SemanticError` does.
    """


class NlqUnavailable(NlqError):
    """The archive is not set up for this, and a person must change a setting.

    No provider configured, an Azure endpoint or deployment left empty, or a
    credential the endpoint refused. Nothing about the question is wrong and
    asking a different one will not help.
    """


class NlqRefused(NlqError):
    """The model did not answer with the filter it was asked for.

    Prose instead of JSON, an object that does not validate, an answer cut off
    by :attr:`~mailarc_analytics.nlq.config.LlmConfig.max_output_tokens`, or a
    refusal. Permanent for *this* request and not worth retrying — the same
    question to the same model produces the same shape — but a different
    question may well work, which is what separates it from
    :class:`NlqUnavailable`.
    """


NO_LLM = "Natural-language search: no interpreter model is configured."
"""The message every interpreter-off surface shows, word for word.

One constant rather than one sentence per caller, for the reason
:data:`~mailarc_analytics.semantic.errors.NO_EMBEDDER` is one: the search page,
the mode selector and any tool all reach this state, and three hand-written
variants would be three chances to describe it differently.

Short, and with the remedy deliberately left off. A reader meets this at the
moment they wanted an answer, and a paragraph there is read as an obstacle
rather than as help — see the note in
:mod:`mailarc_analytics.semantic.errors`. :data:`CONFIGURE_LLM` is what the
surfaces that *can* act on a remedy add to it.
"""

SETTINGS_PAGE = "/admin/embedder"
"""Where a person changes the interpreter.

The same route the embedder's remedies name — one page holds both models —
and it keeps its old path although the page is now called Models: that path is
quoted in every remedy sentence semantic search shows and pinned by
``test_ui_pages.py``.

A route rather than a setting name, for
:data:`~mailarc_analytics.semantic.errors.SETTINGS_PAGE`'s reason: on the
desktop bundle there is no shell into the application's environment, so a
sentence whose only remedy was an environment variable would be one its reader
could not act on.
"""

CONFIGURE_LLM = (
    f"Choose one on the {SETTINGS_PAGE} page, or set app.llm.provider in "
    "configuration/config.yaml, to ollama, openai or azure_openai."
)
"""The remedy, for the one surface with room to print it.

The page first and the file second, because the page is the one every
installation can reach. ``ollama`` is listed first among the three on purpose:
it is the only one that needs no account.
"""


def refused(answer: str) -> str:
    """The sentence for a model that answered something other than a filter.

    Quotes what came back rather than describing it, which is the same call
    every "we could not read this" message in this repository makes: the reader
    can see whether the model refused, rambled or is simply the wrong model,
    and none of those three is a guess this function is in a position to make.
    """
    seen = " ".join(answer.split())[:200]
    return (
        "Natural-language search: the model did not answer with a filter. "
        f"It said: {seen!r}"
    )
