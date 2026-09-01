"""The search page's state: a filled form, a page of results, one open mail.

The archive answers through :class:`~mailarc_core.ArchiveReader` and — for the
semantic path — :class:`~mailarc_analytics.semantic.SemanticSearch`, both read
out of the service registry inside the method that needs one (§6). Reading the
message a row names is not here at all: it is
:class:`~mailarc_ui.message_detail.MessageDetailState`, a mixin this state
lists, so filling the list and reading one of its rows stay separable.

Three things are worth knowing before editing this file.

**Every read is a background handler.** The graph, the disk and — on the
semantic path — an HTTP round trip to a model that may be cold all sit behind
one button, and a plain handler would hold this client's state lock for the
whole of it. So the lock is taken around the two mutations and never around
the read, and what crosses between them is one frozen
:class:`~mailarc_ui.search.model.SearchAnswer` rather than six assignments a
later edit could leave half-applied.

**Semantic search is question-only.** The KNN ranks by meaning over the whole
index; it cannot honour a sender, a date range or an account. So the form
narrows to its one field, :meth:`MailSearchState._asked` drops the rest rather
than pretending, and the page says so — a form that quietly stops honouring
what is typed in it is the one way a search can lie about what it searched.

**Switching path throws the previous answer away.** A full-text relevance and
a cosine similarity are different measurements on the same ``0..1`` column,
and leaving rows on screen under a selector that now says something else
invites a reader to believe the numbers moved when only the label did.
"""

import asyncio
import logging
from datetime import UTC, datetime

import reflex as rx
from reflex.event import EventCallback

from mailarc_analytics.semantic import (
    NO_EMBEDDER,
    SearchKind,
    SearchRequest,
    SemanticError,
)
from mailarc_core.archive.search import SearchFilters
from mailarc_ui.message_detail.state import MessageDetailState
from mailarc_ui.search import reads
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    MODE_FULLTEXT,
    MODE_SEMANTIC,
    SEARCH_FAILED,
    ResultRow,
    SearchAnswer,
    filters_of,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
"""How many rows one read brings in. The list scrolls; "more" appends a page."""

SEMANTIC_HITS = PAGE_SIZE
"""How deep the KNN goes. One ranked answer, not a paged one: a nearest-
neighbour search takes a ``k`` and no offset, so asking again for "the next
fifty" would mean re-ranking the first fifty to throw them away."""

FIELD_LIMIT = 200
"""How much text one field keeps.

An event is addressable by name and its arguments are whatever the caller
sent, so this is not about what a form can hold — it is the cut that stops a
megabyte from reaching a Cypher parameter or a RediSearch query.
"""

_EMPTY = SearchFilters()
"""The form nobody has filled in — the browse-the-newest read."""


class MailSearchState(MessageDetailState, rx.State):
    """The search page: the form, what it found, and the message being read.

    The mixin comes first in the bases, so what it defines is copied into this
    state before ``rx.State`` makes it a real one.
    """

    query: str = ""
    mode: str = MODE_FULLTEXT
    sender: str = ""
    recipient: str = ""
    date_from: str = ""
    date_to: str = ""
    attachments: str = ATTACH_ANY
    account_id: str = ""

    accounts: list[dict[str, str]] = []
    """What the account picker offers — ``value`` is the account's row id as a
    string, which is what the graph keys an ``Account`` node under."""

    rows: list[ResultRow] = []
    total: int = 0
    """How many messages match, or ``0`` when nothing counted them — see
    :attr:`~mailarc_ui.search.model.SearchAnswer.total`."""

    offset: int = 0
    """Where the next page starts. Held rather than derived from the row count
    because the two part company the moment a read fails half way."""

    searching: bool = False
    error: str = ""
    notice: str = ""
    """A statement about the question or the configuration, shown as written —
    as against :attr:`error`, which is a fault and never says whose."""

    searched: bool = False
    """Whether anything has been asked yet. What tells "nothing matched this"
    apart from "nothing is archived", which are two different empty lists."""

    semantic_ready: bool = False
    semantic_note: str = ""
    """Why the semantic segment is off, beside the selector while it is."""

    _answered: SearchFilters = _EMPTY
    """The question the rows on screen came from.

    Backend-only, and the reason it exists is "Load more". The form goes on
    being editable while an answer is up, so paging off the *current* form
    would append page two of a search nobody ran to page one of the search
    they are looking at. A page keeps belonging to the question that produced
    it.
    """

    @rx.var
    def has_rows(self) -> bool:
        return len(self.rows) > 0

    @rx.var
    def has_more(self) -> bool:
        """Whether asking for another page could bring anything.

        Two answers, because only half the reads count. Where a total is known
        the question is arithmetic. Where it is not — a full-text answer is
        ranked and deliberately un-counted — a page that came back *full* is
        the only evidence there is that another one exists, which costs at
        most one empty read when the archive holds an exact multiple.
        """
        if self.total > 0:
            return self.offset < self.total
        return self.offset > 0 and self.offset % PAGE_SIZE == 0

    @rx.var
    def count_label(self) -> str:
        """How many of how many, for the strip above the list."""
        shown = len(self.rows)
        if shown == 0:
            return "No messages"
        if self.total > shown:
            return f"{shown} of {self.total}"
        return f"{shown}"

    @rx.var
    def semantic_chosen(self) -> bool:
        """Whether the structured half of the form is being ignored."""
        return self.mode == MODE_SEMANTIC

    @rx.var
    def mode_options(self) -> list[dict[str, str | bool]]:
        """The two segments, the second dead until an embedder exists.

        Built here rather than in the form because only the state knows
        whether the semantic path can answer, and an enabled control over a
        path that always fails is a promise.
        """
        return [
            {"label": "Fulltext", "value": MODE_FULLTEXT},
            {
                "label": "Semantic",
                "value": MODE_SEMANTIC,
                "disabled": not self.semantic_ready,
            },
        ]

    @rx.var
    def can_search(self) -> bool:
        """Whether pressing Search could produce anything.

        The vars are read one by one rather than through
        :meth:`_asked`, because this is what Reflex watches to decide when to
        recompute: a dependency reached through a helper is a dependency that
        can go missing when the helper changes.
        """
        if self.searching:
            return False
        if self.mode == MODE_SEMANTIC:
            return self.semantic_ready and self.query.strip() != ""
        return bool(
            self.query.strip()
            or self.sender.strip()
            or self.recipient.strip()
            or self.date_from.strip()
            or self.date_to.strip()
            or self.account_id.strip()
            or self.attachments != ATTACH_ANY
        )

    @rx.var
    def nothing_matched(self) -> bool:
        """An empty list that is an answer, rather than an empty archive."""
        return self.searched and len(self.rows) == 0

    @rx.event
    def set_query(self, value: str) -> None:
        self.query = _kept(value)

    @rx.event
    def search_on_enter(self, key: str) -> EventCallback[()] | None:
        """Enter in the question box does what the button does, nothing else.

        Guarded by the same computed var the button is disabled by, so a
        keystroke cannot run a search a click could not.
        """
        if key != "Enter" or not self.can_search:
            return None
        return MailSearchState.submit

    @rx.event
    def set_sender(self, value: str) -> None:
        self.sender = _kept(value)

    @rx.event
    def set_recipient(self, value: str) -> None:
        self.recipient = _kept(value)

    @rx.event
    def set_date_from(self, value: str) -> None:
        self.date_from = _kept(value)

    @rx.event
    def set_date_to(self, value: str) -> None:
        self.date_to = _kept(value)

    @rx.event
    def choose_attachments(self, value: str) -> None:
        """One of the three positions; anything else asks for either."""
        self.attachments = (
            value if value in (ATTACH_WITH, ATTACH_WITHOUT) else ATTACH_ANY
        )

    @rx.event
    def choose_account(self, value: str) -> None:
        """A mailbox to search in; an id nobody offered filters nothing."""
        offered = {one["value"] for one in self.accounts}
        self.account_id = value if value in offered else ""

    @rx.event
    def choose_mode(self, value: str) -> None:
        """Switch path, and drop the answer the other one gave.

        The value arrives over the socket, so it is checked rather than
        trusted — and the semantic path cannot be chosen while it is off,
        whatever a caller sends. Choosing the path that is already chosen
        changes nothing, answer included: throwing the rows away is what
        *switching* costs, not what touching the control costs.
        """
        chosen = (
            MODE_SEMANTIC
            if value == MODE_SEMANTIC and self.semantic_ready
            else MODE_FULLTEXT
        )
        if chosen == self.mode:
            return
        self.mode = chosen
        self._forget()

    @rx.event
    def reset_form(self) -> EventCallback[()]:
        """Empty the form and go back to the newest messages.

        Not ``reset``: that name is a method every Reflex state already has,
        and an event handler is refused at class-creation time for shadowing
        one.
        """
        self.query = ""
        self.sender = ""
        self.recipient = ""
        self.date_from = ""
        self.date_to = ""
        self.attachments = ATTACH_ANY
        self.account_id = ""
        self.mode = MODE_FULLTEXT
        self._forget()
        return MailSearchState.load

    @rx.event
    async def select(self, message_id: str) -> None:
        """Open the message a row names; an id from nowhere is ignored."""
        row = next((one for one in self.rows if one.id == message_id), None)
        if row is None:
            return
        await self._open_message(message_id, row.eml_sha256)

    @rx.event(background=True)
    async def load(self) -> None:
        """What this page can offer, and the newest messages. Its ``on_load``.

        Three reads that fail apart: whether the semantic path is available is
        answered in memory, the account picker comes out of the archive's own
        database, and the listing comes off the graph. A graph that is not
        running has to leave the form usable, and a database that is not has
        to leave the listing readable.
        """
        async with self:
            self.searching = True
            self.error = ""
            self.notice = ""
            self.searched = False
            self._answered = _EMPTY
        ready, note = _semantic_offer()
        accounts = await _accounts()
        answer = await _answer(MODE_FULLTEXT, _EMPTY, 0)
        async with self:
            self.semantic_ready = ready
            self.semantic_note = note
            self.accounts = accounts
            if not ready and self.mode == MODE_SEMANTIC:
                self.mode = MODE_FULLTEXT
            self._apply(answer, append=False)
            self.searching = False

    @rx.event(background=True)
    async def submit(self) -> None:
        """Ask the archive what the form says. The Search button.

        The list is emptied before the read rather than after it: a search
        that fails must not leave the previous answer on screen under a form
        that now says something else.
        """
        async with self:
            if self.searching:
                return
            self.searching = True
            self.searched = True
            self.error = ""
            self.notice = ""
            self.rows = []
            self.total = 0
            self.offset = 0
            self._clear_selection()
            mode, filters = self.mode, self._asked()
            self._answered = filters
        answer = await _answer(mode, filters, 0)
        async with self:
            self._apply(answer, append=False)
            self.searching = False

    @rx.event(background=True)
    async def load_more(self) -> None:
        """Append the next page; the list keeps what it has.

        The question is :attr:`_answered` and not the form: a page belongs to
        the search that produced the page above it, whatever has been typed
        since.

        Never on the semantic path. :data:`SEMANTIC_HITS` is the whole of that
        answer, so :attr:`has_more` is already false there — the second half
        of the guard is what keeps it false if that ever stops being true.
        """
        async with self:
            if self.searching or not self.has_more or self.mode == MODE_SEMANTIC:
                return
            self.searching = True
            filters, offset = self._answered, self.offset
        answer = await _answer(MODE_FULLTEXT, filters, offset)
        async with self:
            self._apply(answer, append=True)
            self.searching = False

    def _asked(self) -> SearchFilters:
        """The form as the archive reads it.

        On the semantic path that is the question and nothing else — see this
        module's docstring — so the structured fields are dropped here rather
        than left to a reader to notice they had no effect.
        """
        if self.mode == MODE_SEMANTIC:
            return filters_of(query=self.query)
        return filters_of(
            query=self.query,
            sender=self.sender,
            recipient=self.recipient,
            date_from=self.date_from,
            date_to=self.date_to,
            attachments=self.attachments,
            account_id=self.account_id,
        )

    def _apply(self, answer: SearchAnswer, *, append: bool) -> None:
        """One answer, all of it, at once.

        A failed read leaves the rows alone: on a fresh search there are none
        to leave, and on a "load more" the page a reader is looking at is
        still the page they asked for.
        """
        self.error = answer.error
        self.notice = answer.notice
        if answer.error:
            return
        self.rows = [*self.rows, *answer.rows] if append else list(answer.rows)
        self.total = answer.total
        self.offset = len(self.rows)
        if self.selected_id not in {row.id for row in self.rows}:
            self._clear_selection()

    def _forget(self) -> None:
        """Drop the last answer, all of it, and the message it was showing."""
        self._answered = _EMPTY
        self.rows = []
        self.total = 0
        self.offset = 0
        self.error = ""
        self.notice = ""
        self.searched = False
        self._clear_selection()

    def _clear_selection(self) -> None:
        self.selected_id = ""
        self._clear_views()


def _kept(value: str) -> str:
    """As much of a typed field as this page carries. See :data:`FIELD_LIMIT`."""
    return value[:FIELD_LIMIT]


def _semantic_offer() -> tuple[bool, str]:
    """Whether the semantic segment can be offered, and why not when it cannot.

    Deliberately touches no graph. Both questions — is there a service, does
    it have an embedder — are answered in memory, so the page can say
    "semantic search is off" on an installation whose graph is not even
    running. The sentence is
    :data:`~mailarc_analytics.semantic.errors.NO_EMBEDDER`, which names the
    setting to change.
    """
    try:
        search = reads.semantic_search()
    except RuntimeError as error:
        logger.warning("The search page found no search service")
        return False, str(error)
    return search.available, "" if search.available else NO_EMBEDDER


async def _accounts() -> list[dict[str, str]]:
    """The account picker's options, or an empty picker.

    A failure here is not a failure of the page: the account filter is one
    optional field, and losing the database must not cost the listing beside
    it. Logged with its traceback, shown as nothing.
    """
    try:
        return await reads.account_options()
    except Exception:
        logger.exception("Could not read the accounts for the search picker")
        return []


async def _answer(mode: str, filters: SearchFilters, offset: int) -> SearchAnswer:
    """One search, with every way it can fail turned into a sentence.

    Two clauses, and they are the taxonomy this page needs. A
    :class:`~mailarc_analytics.semantic.errors.SemanticError` — or the
    ``ValueError`` the core's own sanitizer raises for the same reason — was
    written for the person reading it: it names the setting to change, or says
    the question holds no searchable words. Those are shown verbatim as a
    notice, because they are statements about the ask rather than faults.
    Anything else is a fault: the graph went away, the embedder's host refused
    the connection, a driver raised. Those are logged with a traceback and
    shown as :data:`~mailarc_ui.search.model.SEARCH_FAILED`, because a
    driver's words can name a path inside this installation.
    """
    try:
        if mode == MODE_SEMANTIC:
            return await _nearest(filters.text)
        return await asyncio.to_thread(_filtered, filters, offset)
    except (SemanticError, ValueError) as error:
        logger.info("The archive cannot answer this search: %s", error)
        return SearchAnswer(notice=str(error))
    except Exception:
        logger.exception("The archive could not be searched")
        return SearchAnswer(error=SEARCH_FAILED)


def _filtered(filters: SearchFilters, offset: int) -> SearchAnswer:
    """One page of whatever the form asked for. Blocking — hence the thread."""
    page = reads.archive_reader().search_messages(
        filters, limit=PAGE_SIZE, offset=offset
    )
    now = datetime.now(UTC)
    return SearchAnswer(
        rows=tuple(
            ResultRow.from_summary(one.summary, now, one.relevance) for one in page.hits
        ),
        total=page.total or 0,
    )


async def _nearest(question: str) -> SearchAnswer:
    """The KNN's ranking, hydrated into rows in exactly that order.

    Two round trips on purpose. The embedding is HTTP and is awaited; the
    hydration is a graph read and goes to a thread. A message the ranking
    names but the graph no longer holds is left out rather than answered with
    a hole, which is :meth:`~mailarc_core.ArchiveReader.messages_by_ids`'s
    contract — so the row count can be shorter than the hit count.

    ``notice`` carries the result's coverage sentence: a semantic answer over
    a half-embedded archive is short for a reason the rows cannot show.
    """
    result = await reads.semantic_search().semantic(
        SearchRequest(text=question, kind=SearchKind.SEMANTIC, limit=SEMANTIC_HITS)
    )
    scores = {hit.message_id: hit.score for hit in result.hits}
    summaries = await asyncio.to_thread(
        reads.archive_reader().messages_by_ids, list(scores)
    )
    now = datetime.now(UTC)
    rows = tuple(
        ResultRow.from_summary(one, now, scores.get(one.id)) for one in summaries
    )
    return SearchAnswer(rows=rows, total=len(rows), notice=result.notice)
