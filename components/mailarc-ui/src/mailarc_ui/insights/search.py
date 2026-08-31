"""Finding a message on the page that says what the archive is made of.

Two paths behind one box. Full text reads the index the baseline migration
created and needs nothing configured; semantic search needs an embedder, a
vector index and a finished embed job. The panel offers both and never blurs
them: the scores mean different things, so switching path throws the previous
answer away rather than leaving a ranking on screen that the other index did
not produce.

**The state a fresh installation is in is the interesting one.** ``provider``
defaults to ``none`` (§7.4), so semantic search is off for everybody who has
not configured a model, and what the panel does about that is the phase's
stated requirement: it says so, in the sentence
:data:`~mailarc_analytics.semantic.errors.NO_EMBEDDER` — which names the
setting to change — and it keeps the full-text half working. An empty result
list is a *valid answer* to a search, so a user handed one concludes their
archive holds nothing on the subject and stops looking.

A state of its own rather than three more vars on
:class:`~mailarc_ui.insights.state.AnalyticsInsightsState`, for two reasons
that both outlive the file size. This panel reads through a different service
— :class:`~mailarc_analytics.semantic.search.SemanticSearch`, not the
``AnalyticsReader`` — so an archive nobody has rebuilt, or a derived layer
that failed to read, leaves the search working; and a search is not an
analysis, so it must not sit behind the readout's spinner or be re-run by its
Refresh.
"""

import logging

import reflex as rx
from appkit_commons.registry import service_registry
from reflex.event import EventCallback

from mailarc_analytics.semantic import (
    DEFAULT_HITS,
    NO_EMBEDDER,
    SearchKind,
    SearchRequest,
    SemanticError,
    SemanticSearch,
)
from mailarc_ui.insights.model import Found, HitView

logger = logging.getLogger(__name__)

SEARCH_PATHS = [
    {"label": "Full text", "value": SearchKind.FULLTEXT.value},
    {"label": "Semantic", "value": SearchKind.SEMANTIC.value},
]
"""The two paths, as the selector above the box lists them.

Worded exactly as the errors are — "Semantic search is off …" reads as an
answer about the control the user just moved — and built from
:class:`~mailarc_analytics.semantic.model.SearchKind` so a third path cannot
appear in one place and not the other.
"""

NOTHING_ASKED = "Type a word or two to search for."

SEARCH_FAILED = (
    "The archive could not be searched right now. Its database or the "
    "embedding service did not answer — check that the mail archive is "
    "running, then try again. The details are in the application log."
)
"""What a fault looks like on screen, and never the exception's own text.

A driver's message can carry a filesystem path out of this installation — a
blob-store failure names ``…/mailstore/ab/cd.eml`` — and this panel renders
whatever it is given into a browser. The MCP server made exactly this choice
for exactly this class of error, with a test asserting that "mailstore" never
crosses the wire; two surfaces over one archive have no business disagreeing
about it. Nothing is lost: :meth:`ArchiveSearchState._answer` logs the
exception with its traceback before showing this.

Distinct from a :class:`~mailarc_analytics.semantic.errors.SemanticError`,
which *is* shown verbatim — those sentences were written for the person
reading them and name the setting to change.
"""

NO_SEARCH = (
    "Search is not wired up in this build — no SemanticSearch is registered. "
    "The composition root publishes it; did app.composition run?"
)
"""The developer's error, in the one place a user could meet it.

Its own sentence rather than a bare ``KeyError``: the panel cannot tell a
half-wired application apart from a broken one, and the two have completely
different fixes.
"""


def archive_search() -> SemanticSearch:
    """The search the composition root published. Call inside a method only.

    Read out of the registry the way
    :func:`~mailarc_ui.insights.state.analytics_reader` reads its reader:
    ``mailarc-ui`` may not import ``app`` (§4.1), and the embedder behind this
    object is built from configuration, which only the composition root is
    allowed to do.
    """
    try:
        return service_registry().get(SemanticSearch)
    except KeyError as error:
        raise RuntimeError(NO_SEARCH) from error


class ArchiveSearchState(rx.State):
    """The search box, its two paths, and what it says when one is off.

    ``ready`` and ``semantic_ready`` answer two different questions and both
    are needed. The first is whether a search can run at all — there is a
    service to run it — and the second is whether the *semantic* path can,
    which is a statement about configuration that the panel makes before
    anybody presses a button.
    """

    query: str = ""
    kind: str = SearchKind.FULLTEXT.value
    """Which path the next search takes. A plain string and not the enum: it
    arrives from the browser, and :meth:`_path` is what turns it back into one
    without trusting it."""

    hits: list[HitView] = []
    summary: str = ""
    notice: str = ""
    error: str = ""
    error_color: str = "red"

    searching: bool = False
    ready: bool = False
    """Whether a search service was found. False to begin with, so the button
    is dead until :meth:`prepare` has said otherwise — an enabled control over
    an unwired panel is a promise."""

    semantic_ready: bool = False
    semantic_note: str = ""
    """Why the semantic path is off, shown beside the selector while it is.

    Empty when it is on. Held in a var rather than written into the component
    because the reason can be either "no embedder is configured" or "nothing
    is registered", and only the state knows which.
    """

    embedding_model: str = ""
    """The model the vectors are in, for the badge beside the selector. A
    semantic result is only comparable with the vectors of one model, so the
    page names the one it is searching under."""

    @rx.var
    def semantic_chosen(self) -> bool:
        return self.kind == SearchKind.SEMANTIC.value

    @rx.var
    def semantic_blocked(self) -> bool:
        """The path the user picked cannot answer, and the note says why."""
        return self.semantic_chosen and not self.semantic_ready

    @rx.var
    def can_search(self) -> bool:
        """Whether pressing the button could produce anything.

        A disabled button beside a sentence naming the setting to change is a
        clearer statement than a live button that always fails.
        """
        return (
            self.ready
            and not self.searching
            and not self.semantic_blocked
            and self.query.strip() != ""
        )

    @rx.event
    def set_query(self, value: str) -> None:
        self.query = value

    @rx.event
    def choose_path(self, kind: str) -> None:
        """Switch path, and drop the answer the other one gave.

        Not tidiness. A full-text relevance and a cosine similarity are
        different measurements on the same 0–1 column, and leaving twenty rows
        on screen under a selector that now says something else invites a
        reader to believe the numbers moved when only the label did.
        """
        self.kind = kind
        self._forget()

    @rx.event
    def search_on_enter(self, key: str) -> EventCallback[()] | None:
        """Enter in the box does what the button does, and nothing else does."""
        if key != "Enter" or not self.can_search:
            return None
        return ArchiveSearchState.run

    @rx.event(background=True)
    async def prepare(self) -> None:
        """What this panel can offer, asked once when it mounts.

        Wired to the card's ``on_mount`` rather than to the page's
        ``on_load``: the page primes the analytics readout there, and a panel
        that reads through a different service should not have to be added to
        somebody else's chain to know whether it works.

        Deliberately touches no graph. Both questions it asks — is there a
        service, does it have an embedder — are answered in memory, so the
        panel can say "semantic search is off" on an installation whose graph
        is not even running.
        """
        async with self:
            try:
                search = archive_search()
            except RuntimeError as error:
                logger.warning("The search panel found no search service")
                self.ready = False
                self.semantic_ready = False
                self.error, self.error_color = str(error), "red"
                return
            self.ready = True
            self.semantic_ready = search.available
            self.embedding_model = search.model
            self.semantic_note = "" if search.available else NO_EMBEDDER
            self.error = ""

    @rx.event(background=True)
    async def run(self) -> None:
        """Search the archive, off the state lock.

        A background task for the reason the readout next door is one: the
        semantic path embeds the query over HTTP against a model that may be
        cold, and a plain handler holds this client's state lock for the whole
        round trip — which would freeze every other event on the session
        behind somebody's search.
        """
        async with self:
            asked, kind = self.query.strip(), self.kind
            if not asked:
                self._apply(Found.failed(NOTHING_ASKED, color="yellow"))
                return
            self.searching = True
            self.error = ""
        found = await self._answer(asked, kind)
        async with self:
            self._apply(found)
            self.searching = False

    async def _answer(self, asked: str, kind: str) -> Found:
        """One search, with every way it can fail turned into a sentence.

        The two ``except`` clauses are the taxonomy this panel needs and no
        more. A :class:`~mailarc_analytics.semantic.errors.SemanticError` was
        written for the person reading it — it names the setting or says the
        query has no searchable words in it — so it is shown verbatim and in
        yellow, because it is a statement about the configuration rather than
        a fault. Anything else is a fault: the graph went away, the embedder's
        host refused the connection, a driver raised. Those are red, and
        logged with a traceback — and shown as :data:`SEARCH_FAILED` rather
        than as their own text, because a driver's words can name a path
        inside this installation and this string is rendered into a browser.
        """
        try:
            search = archive_search()
            request = SearchRequest(
                text=asked, kind=self._path(kind), limit=DEFAULT_HITS
            )
            result = await search.search(request)
        except SemanticError as error:
            logger.info("The archive cannot answer this search: %s", error)
            return Found.failed(str(error), color="yellow")
        except Exception:
            logger.exception("The archive could not be searched")
            return Found.failed(SEARCH_FAILED)
        return Found.from_result(result, asked=asked, limit=DEFAULT_HITS)

    def _path(self, kind: str) -> SearchKind:
        """The chosen path, without trusting the string that named it.

        ``SearchKind(kind)`` would raise on anything the selector did not
        produce, and this var arrives over the socket — an event is
        addressable by name and its arguments are whatever the caller sent. So
        anything that is not exactly the semantic path falls back to the one
        that always works.
        """
        if kind == SearchKind.SEMANTIC.value:
            return SearchKind.SEMANTIC
        return SearchKind.FULLTEXT

    def _forget(self) -> None:
        """Drop the last answer, all of it at once."""
        self.hits = []
        self.summary = ""
        self.notice = ""
        self.error = ""

    def _apply(self, found: Found) -> None:
        """One search's answer, all of it, at once."""
        self.hits = found.hits
        self.summary = found.summary
        self.notice = found.notice
        self.error = found.error
        self.error_color = found.error_color
