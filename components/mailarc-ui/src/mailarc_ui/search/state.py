"""The search page's state: a filled form, a page of results, one open mail.

The archive answers through :class:`~mailarc_core.ArchiveReader` and — for the
semantic path — :class:`~mailarc_analytics.semantic.SemanticSearch`, both read
out of the service registry inside the method that needs one (§6). Reading the
message a row names is not here at all: it is
:class:`~mailarc_ui.graph.ChipGraphState` — the reading pane's own mixin with
its header chips wired to a picture — which this state lists, so filling the
list and reading one of its rows stay separable.

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

from mailarc_analytics.nlq import ResolvedQuery
from mailarc_core.archive.search import KnownAddresses, SearchFilters
from mailarc_ui.graph import ChipGraphState
from mailarc_ui.search.answers import (
    PAGE_SIZE,
    _accounts,
    _addresses,
    _answer,
    _attachment_catalogue,
    _conversation_rows,
    _directory,
    _interpreted,
    _llm_offer,
    _memberships_of,
    _semantic_offer,
    _whole_conversations,
)
from mailarc_ui.search.files import (
    kind_options,
    names_of,
    offered_kinds,
    types_of,
)
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    MATCH_ALL,
    MATCH_ANY,
    MODE_FULLTEXT,
    MODE_NATURAL,
    MODE_SEMANTIC,
    READ_GROUPINGS,
    Grouping,
    ListLine,
    Membership,
    ResultRow,
    SearchAnswer,
    filters_of,
    grouping_of,
    lines_of,
)
from mailarc_ui.search.natural import (
    attachment_segment,
    understood,
    vocabulary_of,
)
from mailarc_ui.search.ordering import alphabetical

logger = logging.getLogger(__name__)

FIELD_LIMIT = 200
"""How much text one field keeps.

An event is addressable by name and its arguments are whatever the caller
sent, so this is not about what a form can hold — it is the cut that stops a
megabyte from reaching a Cypher parameter or a RediSearch query.
"""

ADDRESS_LIMIT = 20
"""How many addresses one picker carries.

The same reasoning as :data:`FIELD_LIMIT` and a sharper reason besides: on the
recipients' *all* reading every address is a pattern of its own in the
statement, so a caller sending a thousand of them would build a
thousand-clause query. Twenty is far past what either field is drawn to hold
and far short of that.
"""

_EMPTY = SearchFilters()
"""The form nobody has filled in — the browse-the-newest read."""


class MailSearchState(ChipGraphState, rx.State):
    """The search page: the form, what it found, and the message being read.

    The mixin comes first in the bases, so what it defines is copied into this
    state before ``rx.State`` makes it a real one. It is
    :class:`~mailarc_ui.graph.ChipGraphState` rather than the reading pane's own
    :class:`~mailarc_ui.message_detail.MessageDetailState` — the same pane with
    the header's chips wired to a picture, which is what fills the third tab.
    """

    query: str = ""
    mode: str = MODE_FULLTEXT
    senders: list[str] = []
    """The addresses picked under **From**, meaning *any of these*.

    There is no switch beside it the way there is beside :attr:`recipients`,
    because a message has exactly one sender: *from all of these* is a question
    with no answer.
    """

    recipients: list[str] = []
    """The addresses picked under **To**. Several, hence :attr:`recipient_match`."""

    recipient_match: str = MATCH_ALL
    """Whether **To** means *all of these* or *any of these* — the switch over
    that field. See :data:`~mailarc_ui.search.model.MATCH_ALL`."""

    date_from: str = ""
    date_to: str = ""
    attachments: str = ATTACH_ANY

    file_kinds: list[str] = []
    """The file families picked under **File type**, each a
    :class:`~mailarc_ui.message_detail.FileKind` value.

    Families and not content types: what the archive is asked is
    :func:`~mailarc_ui.search.files.types_of`'s expansion of this into the
    types it actually holds, and a form that held the expansion would have to
    re-derive which boxes are ticked from it.
    """

    file_type_options: list[dict[str, str]] = []
    """What the file picker offers — one entry per family this archive holds
    a file of, alphabetically. Dicts and not plain strings, because a family
    is shown as a word and comes back as its enum value."""

    account_id: str = ""

    accounts: list[dict[str, str]] = []
    """What the account picker offers, by name — ``value`` is the account's row
    id as a string, which is what the graph keys an ``Account`` node under."""

    sender_options: list[str] = []
    recipient_options: list[str] = []
    """Every address the archive holds in that role, alphabetically.

    Plain strings, because for an address the option and its label are the same
    thing — unlike the accounts above, where a row id has to be shown as the
    name somebody gave the mailbox.

    Two lists and not one: an address that only ever received mail is not a
    sender, and offering it under **From** would be offering a filter that
    cannot match. The archive answers both in one read — see
    :meth:`~mailarc_core.ArchiveReader.known_addresses`, which hands them over
    busiest first; :mod:`~mailarc_ui.search.ordering` is where that becomes the
    order a person reads in.
    """

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

    llm_ready: bool = False
    llm_note: str = ""
    """Why the natural segment is off, beside the selector while it is."""

    understood: str = ""
    """What the last interpreted question was taken to mean.

    Its own var and deliberately not :attr:`notice`. ``notice`` belongs to an
    *answer* and :meth:`_apply` overwrites it on every read — including the
    read behind **Load more** — so a sentence about the *question* put there
    would vanish the moment somebody paged. It is shown under the **Mode**
    segment beside :attr:`llm_note`, where a statement about how the form got
    filled in belongs.

    Empty most of the time, because the filled-in fields already say what was
    searched. It carries only what they cannot show: a name the archive had no
    address for, a date refused, the words dropped to answer a "wann war die
    letzte".
    """

    grouping: str = Grouping.CONVERSATION.value
    """What the list is grouped by — the **Group by** dropdown's value.

    Conversations by default, because a mail list that does not group is a
    mail list that shows an answer twice: the question and the reply, pages
    apart. A plain string rather than the enum, because it is what the
    dropdown binds and what the socket sends back; :func:`grouping_of` is what
    every reader of it goes through.
    """

    _answered: SearchFilters = _EMPTY
    """The question the rows on screen came from.

    Backend-only, and the reason it exists is "Load more". The form goes on
    being editable while an answer is up, so paging off the *current* form
    would append page two of a search nobody ran to page one of the search
    they are looking at. A page keeps belonging to the question that produced
    it.
    """

    _memberships: dict[str, Membership] = {}
    """Which group each row on screen sits in, keyed by message id.

    Accumulated across pages rather than replaced, because a group's heading
    stays where its first member put it and page two's members join it there
    — and emptied on a switch, because a thread id is not a topic id.
    """

    _collapsed: set[str] = set()
    """The groups a reader closed. Everything else is open.

    Held as what was *closed* rather than what is open, so a group that
    arrives on a later page is open without anything having to notice it
    arrived.
    """

    _whole: dict[str, list[ResultRow]] = {}
    """Conversations somebody asked to see in full, by conversation id.

    Kept across a grouping toggle: switching the list to messages and back is
    a change of view, not an instruction to forget what was fetched.
    """

    _drained: bool = False
    """Whether a **Load more** has already come back with nothing.

    :attr:`has_more` cannot tell a full last page from a full page with more
    behind it when the answer carries no total, which every ranked full-text
    answer does. Its arithmetic — an offset that is an exact multiple of
    :data:`PAGE_SIZE` — then stays true after the empty read as well, because
    :meth:`_apply` derives the offset from the rows and an empty page adds
    none. So the button stayed lit and every further press re-issued the same
    empty read. This remembers the one that came back empty.
    """

    _expanding: str = ""
    """The one conversation whose fetch is running. Never :attr:`searching`."""

    _content_types: list[str] = []
    """Every content type this archive's files state, commonest first.

    Backend-only: it is half the *expansion table* behind the file picker, not
    something the browser draws, and it is read once with the address pickers
    for the same reason — it costs a traversal of every attachment edge.
    """

    _untyped_names: list[str] = []
    """The names of the files whose sender stated no type, commonest first.

    The other half of the same table, and the reason a person picking **PDF**
    finds the invoices that arrived as ``application/octet-stream`` — see
    :mod:`mailarc_ui.search.files`.
    """

    @rx.var
    def has_rows(self) -> bool:
        return len(self.rows) > 0

    @rx.var
    def lines(self) -> list[ListLine]:
        """The list as it is drawn — headings, members, and plain rows.

        Every value it needs is read here as a plain attribute rather than
        through a helper, for the reason :attr:`can_search` gives: Reflex
        watches the body of a computed var to decide when to recompute it, and
        a dependency reached through a method is one it can lose. The rules
        themselves are :func:`~mailarc_ui.search.model.lines_of`'s, which is
        pure and knows nothing about any of this.
        """
        return lines_of(
            self.rows,
            self._memberships,
            grouping=grouping_of(self.grouping),
            collapsed=self._collapsed,
            whole=self._whole,
            busy=self._expanding,
        )

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
        if self._drained:
            return False
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
        """Whether the structured half of the form is being ignored.

        Semantic alone. The natural segment is the opposite case — it *writes*
        those fields and leaves them editable — so it must not join this var,
        and every ``disabled=`` binding in the form still reads exactly this.
        """
        return self.mode == MODE_SEMANTIC

    @rx.var
    def natural_chosen(self) -> bool:
        """Whether the next search reads the question before it runs."""
        return self.mode == MODE_NATURAL

    @rx.var
    def mode_options(self) -> list[dict[str, str | bool]]:
        """The three segments, each dead until the thing it needs exists.

        Built here rather than in the form because only the state knows
        whether a path can answer, and an enabled control over a path that
        always fails is a promise.

        **Natural is appended, never inserted.** Full text is first because it
        is the one that always works, and the other two keep the positions they
        already had — a form test asserts on the second segment by index, and
        more to the point a selector whose options move under a reader is a
        control they stop trusting.
        """
        return [
            {"label": "Fulltext", "value": MODE_FULLTEXT},
            {
                "label": "Semantic",
                "value": MODE_SEMANTIC,
                "disabled": not self.semantic_ready,
            },
            {
                "label": "Natural",
                "value": MODE_NATURAL,
                "disabled": not self.llm_ready,
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
        if self.mode == MODE_NATURAL:
            # The one field this mode reads. A structured filter is what it
            # *produces*, so a form with a sender picked and no sentence in it
            # has nothing to interpret.
            return self.llm_ready and self.query.strip() != ""
        return bool(
            self.query.strip()
            or self.senders
            or self.recipients
            or self.date_from.strip()
            or self.date_to.strip()
            or self.account_id.strip()
            or self.attachments != ATTACH_ANY
            or self.file_kinds
        )

    @rx.var
    def nothing_matched(self) -> bool:
        """An empty list that is an answer, rather than an empty archive."""
        return self.searched and len(self.rows) == 0

    @rx.event
    def set_query(self, value: str) -> None:
        self.query = _kept(value)

    @rx.event
    def clear_query(self) -> None:
        """Empty the question box. The clear button inside it.

        Its own handler rather than ``set_query("")`` bound at the call site,
        because the button is built in ``kit.inputs`` and the kit does not know
        what a search is — it takes the event, not the var.
        """
        self.query = ""

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
    def choose_senders(self, values: list[str]) -> None:
        """The addresses to search for as the sender; an empty list clears it.

        Several of them mean *any*, which is the only thing they can mean —
        see :attr:`senders`.
        """
        self.senders = _offered(values, self.sender_options)

    @rx.event
    def choose_recipients(self, values: list[str]) -> None:
        """The addresses to search for as recipients; an empty list clears it."""
        self.recipients = _offered(values, self.recipient_options)

    @rx.event
    def choose_recipient_match(self, value: str) -> None:
        """Whether **To** means all of the picked addresses or any of them."""
        self.recipient_match = MATCH_ANY if value == MATCH_ANY else MATCH_ALL

    @rx.event
    def set_date_from(self, value: str) -> None:
        self.date_from = _kept(value)

    @rx.event
    def set_date_to(self, value: str) -> None:
        self.date_to = _kept(value)

    @rx.event
    def choose_file_kinds(self, values: list[str]) -> None:
        """Which kinds of file a message has to carry — any of them.

        Narrowed to what the picker offered, the guard every field on this
        form keeps: the list arrives over the socket, and a family this
        archive holds no files of would answer with an empty result list that
        reads like an empty archive.
        """
        self.file_kinds = offered_kinds(values, self.file_type_options)

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
        trusted — and neither of the two optional paths can be chosen while it
        is off, whatever a caller sends. Anything unrecognised is full text,
        which is the one that always works. Choosing the path that is already
        chosen changes nothing, answer included: throwing the rows away is what
        *switching* costs, not what touching the control costs.
        """
        chosen = self._offered_mode(value)
        if chosen == self.mode:
            return
        self.mode = chosen
        self.understood = ""
        self._forget()

    def _offered_mode(self, value: str) -> str:
        """One mode value narrowed to a path this installation really has.

        The guard every picker on this form keeps, in the one place all three
        segments meet: a value nobody offered would otherwise leave the form in
        a mode whose **Search** button can never light up.
        """
        if value == MODE_SEMANTIC and self.semantic_ready:
            return MODE_SEMANTIC
        if value == MODE_NATURAL and self.llm_ready:
            return MODE_NATURAL
        return MODE_FULLTEXT

    @rx.event
    def reset_form(self) -> EventCallback[()]:
        """Empty the form and go back to the newest messages.

        Not ``reset``: that name is a method every Reflex state already has,
        and an event handler is refused at class-creation time for shadowing
        one.
        """
        self.query = ""
        self.senders = []
        self.recipients = []
        self.recipient_match = MATCH_ALL
        self.date_from = ""
        self.date_to = ""
        self.attachments = ATTACH_ANY
        self.file_kinds = []
        self.account_id = ""
        self.mode = MODE_FULLTEXT
        self._forget()
        return MailSearchState.load

    @rx.event
    async def select(self, message_id: str) -> None:
        """Open the message a row names; an id from nowhere is ignored."""
        row = self._row(message_id)
        if row is None:
            return
        await self._open_message(message_id, row.eml_sha256)

    @rx.event
    def toggle_group(self, group_id: str) -> None:
        """Open or close one group. A group nobody drew is ignored.

        The value arrives over the socket, so it is checked against what is on
        screen — the headings and sections :attr:`lines` is drawing — rather
        than trusted, the same guard :meth:`select` keeps. Against the lines
        and not the memberships, because the bucket a read did not file has a
        section and no membership. Assigned rather than mutated: Reflex tracks
        a var's dirtiness by assignment, so ``set.add`` alone would never reach
        the browser.
        """
        drawn = {one.group_id for one in self.lines if one.is_header or one.is_section}
        if group_id not in drawn:
            return
        if group_id in self._collapsed:
            self._collapsed = self._collapsed - {group_id}
        else:
            self._collapsed = self._collapsed | {group_id}

    @rx.event(background=True)
    async def choose_grouping(self, value: str) -> None:
        """Group the list some other way. The **Group by** dropdown.

        Background because the new grouping may need the memberships of rows
        that are already up — the read is made for the grouping in force, and
        for no other — while the flat list, the sender and the subject write
        one string and read nothing.

        The old memberships and the closed groups go at once: a thread id is
        not a topic id, and a read that then fails leaves the rows in one
        bucket rather than filed under the previous grouping's groups. What
        was fetched for "show whole conversation" stays, because switching
        away and back is a change of view, not an instruction to forget.
        """
        async with self:
            wanted = grouping_of(value)
            if wanted.value == self.grouping:
                return
            self.grouping = wanted.value
            self._memberships = {}
            self._collapsed = set()
            ids = [row.id for row in self.rows] if wanted in READ_GROUPINGS else []
            rows = tuple(self.rows)
        if not ids:
            return
        found = await _memberships_of(ids, wanted)
        whole = await asyncio.to_thread(
            _whole_conversations, rows, found, wanted, datetime.now(UTC)
        )
        async with self:
            if self.grouping != wanted.value:
                return
            self._memberships = {**self._memberships, **found}
            self._whole = {
                **self._whole,
                **{one: list(members) for one, members in whole.items()},
            }

    @rx.event(background=True)
    async def show_whole_conversation(self, conversation_id: str) -> None:
        """Fetch the members this answer left out, for one group.

        Deliberately never touches :attr:`searching`. That var puts the
        list-wide spinner up and takes the Search button away, and asking one
        conversation for the rest of itself is not a search — the group says so
        itself, through :attr:`_expanding`.
        """
        async with self:
            offered = {one.group_id for one in self._memberships.values()}
            if (
                self._expanding
                or self.grouping != Grouping.CONVERSATION
                or conversation_id not in offered
            ):
                return
            self._expanding = conversation_id
        rows, error = await _conversation_rows(conversation_id)
        async with self:
            self._expanding = ""
            if error:
                self.error = error
                return
            self._whole = {**self._whole, conversation_id: list(rows)}

    @rx.event(background=True)
    async def load(self) -> None:
        """What this page can offer, and the newest messages. Its ``on_load``.

        Reads that fail apart: whether the semantic and natural paths are
        available is answered in memory, the account picker comes out of the
        archive's own database, and the address pickers, the directory behind
        them and the listing come off the graph. A graph that is not running
        has to leave the form usable, and a database that is not has to leave
        the listing readable.

        The address pickers are filled here and nowhere else. They are two
        traversals of the whole archive, and a list that is re-read while
        somebody is typing into it is a list that moves under them — so it is
        asked once, when the page opens.
        """
        async with self:
            self.searching = True
            self.error = ""
            self.notice = ""
            self.searched = False
            self._answered = _EMPTY
            self._forget_memberships()
            grouping = grouping_of(self.grouping)
        ready, note = _semantic_offer()
        reads_questions, llm_note = _llm_offer()
        accounts = await _accounts()
        addresses = await _addresses()
        files = await _attachment_catalogue()
        answer = await _answer(MODE_FULLTEXT, _EMPTY, 0, grouping)
        async with self:
            self.semantic_ready = ready
            self.semantic_note = note
            self.llm_ready = reads_questions
            self.llm_note = llm_note
            self.accounts = accounts
            self.sender_options = alphabetical(addresses.senders)
            self.recipient_options = alphabetical(addresses.recipients)
            self._content_types = list(files.content_types)
            self._untyped_names = list(files.untyped_names)
            self.file_type_options = kind_options(
                files.content_types, files.untyped_names
            )
            self.file_kinds = offered_kinds(self.file_kinds, self.file_type_options)
            # Demoted rather than left standing: a mode whose service went
            # away between two page loads is a Search button that can never
            # light up, and nothing on screen would say why.
            self.mode = self._offered_mode(self.mode)
            self._apply(answer, append=False)
            self.searching = False

    @rx.event(background=True)
    async def submit(self) -> None:
        """Ask the archive what the form says. The Search button.

        The list is emptied before the read rather than after it: a search
        that fails must not leave the previous answer on screen under a form
        that now says something else.

        On the natural path there is a step in front of all of this: the
        question is read into the form's own fields first, and what runs
        afterwards is whatever the form then says. So there is one filter
        builder and one search, and the reading is visible in the controls a
        person can correct — see :meth:`_fill_in`.
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
            self._forget_memberships()
            natural, question = self.mode == MODE_NATURAL, self.query
        if natural and not await self._interpret(question):
            return
        async with self:
            mode, filters = self.mode, self._asked()
            grouping = grouping_of(self.grouping)
            self._answered = filters
        answer = await _answer(mode, filters, 0, grouping)
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
            grouping = grouping_of(self.grouping)
        answer = await _answer(MODE_FULLTEXT, filters, offset, grouping)
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
            senders=self.senders,
            recipients=self.recipients,
            recipient_match=self.recipient_match,
            date_from=self.date_from,
            date_to=self.date_to,
            attachments=self.attachments,
            file_types=types_of(self.file_kinds, self._content_types),
            file_names=names_of(self.file_kinds, self._untyped_names),
            account_id=self.account_id,
        )

    async def _interpret(self, question: str) -> bool:
        """Read the question into the form. ``False`` when nothing may run.

        Two awaits outside the lock, which is why this is its own method: the
        directory is a graph scan and the interpretation is an HTTP round trip
        to a model that may be cold, and holding this client's state lock
        across either would freeze the page.

        The directory is read per search rather than kept on the state. It is
        one scan of the ``Address`` label and the model call beside it costs
        orders of magnitude more, while keeping every address the archive holds
        in a Reflex var would be paid for by every session on every page.
        """
        directory = await _directory()
        async with self:
            vocabulary = vocabulary_of(
                directory,
                KnownAddresses(
                    senders=tuple(self.sender_options),
                    recipients=tuple(self.recipient_options),
                ),
                self.file_type_options,
                self.accounts,
            )
        read = await _interpreted(question, vocabulary)
        async with self:
            if isinstance(read, SearchAnswer):
                self._apply(read, append=False)
                self.searching = False
                return False
            self._fill_in(read)
            return True

    def _fill_in(self, read: ResolvedQuery) -> None:
        """Write an interpretation into the form, narrowed exactly as typing is.

        **Every value goes through the guard its own field already keeps.** A
        model is a second source of values arriving from outside this state,
        and a worse one than the browser: it invents things that look right. An
        address that is not in :attr:`sender_options` would not render as a
        pill in the picker at all — so the reader would see an empty **From**
        field over a search that really had been narrowed, which is the exact
        way a form can lie about what it searched.

        The whole form is written, including the fields the reading had no
        opinion about. A leftover date from the last search sitting under a
        freshly interpreted question would be a filter nobody asked for and
        nobody could account for; **all/any** goes back to the form's own
        default for the same reason.
        """
        self.query = _kept(read.text)
        self.senders = _offered(list(read.senders), self.sender_options)
        self.recipients = _offered(list(read.recipients), self.recipient_options)
        self.recipient_match = MATCH_ALL
        self.date_from = read.after
        self.date_to = read.before
        self.attachments = attachment_segment(read.has_attachments)
        self.file_kinds = offered_kinds(list(read.families), self.file_type_options)
        self.account_id = (
            read.account_id
            if read.account_id in {one["value"] for one in self.accounts}
            else ""
        )
        self.understood = understood(read)

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
        self._drained = append and not answer.rows
        self.rows = [*self.rows, *answer.rows] if append else list(answer.rows)
        self.total = answer.total
        self.offset = len(self.rows)
        if answer.grouping == self.grouping:
            self._memberships = {**self._memberships, **answer.memberships}
            self._whole = {
                **self._whole,
                **{one: list(members) for one, members in answer.whole.items()},
            }
        if self._row(self.selected_id) is None:
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
        self._forget_memberships()
        self._clear_selection()

    def _forget_memberships(self) -> None:
        """Drop every grouping the last answer produced.

        A group belongs to the answer that named it. Keeping one across a new
        search would leave a heading standing over a group whose members are
        gone, and a fetched conversation standing over a question that never
        asked for it.
        """
        self._memberships = {}
        self._collapsed = set()
        self._whole = {}
        self._expanding = ""

    def _row(self, message_id: str) -> ResultRow | None:
        """The row an id names, whether the answer brought it or a fetch did.

        Both halves, because a member pulled in by "show the whole
        conversation" is on screen and clickable but was never part of the
        answer — looking only at :attr:`rows` would make it unopenable, and
        would close it again on the next "Load more".
        """
        if not message_id:
            return None
        found = next((one for one in self.rows if one.id == message_id), None)
        if found is not None:
            return found
        for members in self._whole.values():
            found = next((one for one in members if one.id == message_id), None)
            if found is not None:
                return found
        return None

    def _clear_selection(self) -> None:
        self.selected_id = ""
        self._clear_views()


def _kept(value: str) -> str:
    """As much of a typed field as this page carries. See :data:`FIELD_LIMIT`."""
    return value[:FIELD_LIMIT]


def _offered(values: list[str], options: list[str]) -> list[str]:
    """One picker's value, narrowed to what the page actually offered.

    The guard :meth:`MailSearchState.choose_account` keeps, for both address
    fields: the list arrives over the socket, and an address the archive holds
    in neither role could only ever answer with an empty result list — which
    reads like an empty archive rather than like a bad filter.

    Order is kept as the person picked it and duplicates are dropped: one
    address twice is one filter, and on the recipients' *all* reading it would
    otherwise be a second pattern matching the first one's node. Cut at
    :data:`ADDRESS_LIMIT`.
    """
    known = set(options)
    kept: list[str] = []
    for one in values[:ADDRESS_LIMIT]:
        if one in known and one not in kept:
            kept.append(one)
    return kept
