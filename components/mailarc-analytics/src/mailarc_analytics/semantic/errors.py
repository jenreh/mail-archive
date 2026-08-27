"""What semantic search says when it cannot answer — and why it says anything.

The phase's stated requirement is that a semantic search with no embedder
configured produces **a clear error and never an empty result**. The reason is
not tidiness: an empty list is a *valid answer* to a search, and a user reading
one concludes that their archive holds nothing about the thing they looked for.
Every hour they then spend not looking again was bought by us saving an
exception. So the three states in which a vector search cannot run raise, and
each message names the remedy that fixes it — the settings page first and the
configuration file second, in that order, because that is the order of both
reachability and precedence (:data:`SETTINGS_PAGE`, :data:`STORED_WINS`).

Deliberately **not** a :class:`~mailarc_core.mail.errors.MailError`. That
taxonomy answers one question — retry, re-consent, or skip this message — and
none of its three answers fits: nothing was fetched, no credential is stale,
and there is no message to skip. A caller catching ``MailTransientError``
around an import must not accidentally catch "the user has not configured an
embedder" and retry it with backoff forever.

Three classes and no more. :class:`SemanticUnavailable` means *the archive is
not set up for this*, and a human has to change a setting or run a job;
:class:`SearchQueryError` means *this particular query cannot be run*, and the
same caller may sensibly try another one. Both are :class:`SemanticError`, so
an MCP tool or a page can turn the pair into one readable failure with a single
``except``.
"""

SETTINGS_PAGE = "/admin/embedder"
"""Where a person changes the embedder, named in every remedy that has one.

A route rather than a setting name because a route is what most readers can
actually reach: on the desktop bundle there is no shell into the application's
environment at all, so a sentence whose only remedy was an environment variable
was one its reader could not act on.
``components/mailarc-ui/tests/test_ui_pages.py`` pins this against the page's
own ``ROUTE`` so the two cannot drift.
"""

STORED_WINS = (
    "what that page stores overrides the configuration file and the environment"
)
"""The precedence, in one clause, wherever both routes are offered.

Not a detail: once the page has been saved once, the stored row beats
``app_semantic_provider`` — so a reader who follows only the environment half
of these sentences exports a variable, restarts, and meets the identical
message with nothing to show for it.
"""

NO_EMBEDDER = (
    "Semantic search is off: no embedder is configured. Choose one on the "
    f"{SETTINGS_PAGE} page — 'ollama' is a local model needing no account and "
    "no key, 'openai' uploads message text to a third party and needs a key — "
    "then run the embed job to compute the vectors. The same settings are "
    "app_semantic_provider and app_semantic_api_key in the configuration "
    f"file or the environment, but {STORED_WINS}. Full-text search, "
    "correspondents, topics and templates all work without an embedder."
)
"""The message every embedder-off surface shows, word for word.

One constant rather than one sentence per caller: the MCP tool, the insights
panel, the embed job and the search all reach this state, and four
hand-written variants would be four chances to describe the fix wrongly.

It names the page **first** and the environment second, in that order, because
that is the order of both reachability and precedence. It used to name only
``app_semantic_provider``, which was true when the environment was the only way
to configure an embedder and became actively wrong when the settings row
started winning the merge: a user who exported the variable and restarted got
this same sentence back and no explanation.
"""

NO_VECTOR_INDEX = (
    "Semantic search is off: the graph has no vector index on Message."
    "embedding. Run `task graph:upgrade` to apply the migrations, then run "
    "the embed job."
)
"""Vectors may be stored, but nothing can find them.

Its own message because the fix is a different one: the embedder is configured
and works, and the *schema* is behind. Folding it into
:data:`NO_EMBEDDER` would send a user to change a setting that is already
right.
"""


NO_FULLTEXT_INDEX = (
    "Search is off: the graph has no full-text index on Message.subject and "
    "Message.body_text. Run `task graph:upgrade` to apply the migrations."
)
"""The archive cannot be searched at all — the baseline migration is missing.

Separate from :data:`NO_VECTOR_INDEX` because it is a different half of the
schema and a different symptom: this one takes full-text search down too, which
is the path that is supposed to work without any of the semantic setup.
"""


class SemanticError(Exception):
    """Base of everything the semantic package raises on its own behalf."""


class SemanticUnavailable(SemanticError):
    """The archive cannot answer vector questions as it currently stands.

    Not a failure of the search — a statement about the configuration or the
    schema, with the remedy in the message. Callers show it verbatim: it is
    written for the person who has to act on it, and paraphrasing it upstream
    loses the setting name.
    """


class SearchQueryError(SemanticError):
    """This query cannot be run; another one from the same caller might be.

    The full-text path is a second query language reaching the store — the
    caller's words go to RediSearch, which has operators of its own and raises
    on a lone ``(``. The words are tokenised before they get there, so what
    survives to this exception is a query with nothing searchable left in it,
    and the honest answer is "ask differently", not "nothing matched".
    """


def nothing_embedded(*, model: str, total: int) -> str:
    """The message for an archive whose vector index holds nothing yet.

    The state every installation passes through between configuring an embedder
    and finishing the job, and the one this module's whole argument is about: a
    KNN over an empty index answers with an empty list, which is a *valid*
    result for a search, so the user concludes their archive holds nothing on
    the subject and stops looking. The embedder is configured and the index
    exists — what is missing is a job, and this names it.

    Deliberately raised even when every message in the archive is unembeddable.
    Such an archive is rare enough that "run the job and it will tell you it has
    nothing to do" is a better answer than silence, and silence here is
    indistinguishable from a genuine miss.
    """
    return (
        f"Semantic search has nothing to search: none of the {total} messages in "
        f"the archive carries a {model!r} embedding yet. The vector index exists "
        "and the embedder answers — what has not run is the embed job, which "
        "computes the vectors. Run it and ask again. Full-text search works "
        "meanwhile."
    )


def dimension_mismatch(*, index: int, model: str, produced: int) -> str:
    """The message for a live index that does not fit the configured embedder.

    Spelled out rather than summarised, because this is the failure that hides:
    FalkorDB accepts a vector of the wrong length, stores it, and declines to
    index it without an error, a log line or an ``indexingFailures`` count.
    The job would report a hundred thousand messages embedded and the search
    would find none of them.
    """
    return (
        f"The vector index holds {index}-dimensional vectors and the model "
        f"{model!r} produces {produced}. Every vector written would be stored "
        "and never indexed, and no search would find it. Two ways out, and the "
        f"first is usually the one you want: rebuild the index at {produced} "
        f"— the Rebuild the index button on the {SETTINGS_PAGE} page, or "
        "`task graph:reindex-vectors` — and then run the embed job, which "
        "recomputes every vector because a resize forgets them all. Or keep "
        f"the index and set the length to {index} instead, which is a real "
        "choice rather than a fallback for the text-embedding-3-* models: they "
        "are trained so that a prefix of the vector is itself usable, so "
        f"{model!r} will produce {index} floats if asked. The same setting is "
        f"app_semantic_dimension in the configuration file or the "
        f"environment, but {STORED_WINS}."
    )
