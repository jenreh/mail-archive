"""What the search form asks for, and what one result row shows.

Value objects and the formatting that fills them. No I/O, no session and no
Reflex, so all of it is checkable without a graph — the same rule
:mod:`mailarc_ui.message_detail.model` keeps for the pane next door.

Two directions meet here and they are deliberately not symmetric.

**Inwards**, a filled form is eight plain strings, because that is what a
browser sends; :func:`filters_of` is where they become the
:class:`~mailarc_core.archive.search.SearchFilters` the archive understands.
Every one of them is narrowed rather than trusted: a date that will not parse
is no date, an unknown segment is the default one, and a half-typed ``12.``
must not silently empty the result list.

**Outwards**, a :class:`~mailarc_core.archive.model.MessageSummary` becomes a
:class:`ResultRow` whose every field is already a string the browser can
print — a relative time, a set of chips, initials — because §9.1 keeps
anything richer out of a Reflex state.

Outwards has a second step, and it is a pure function for a reason.
:func:`lines_of` turns the flat answer into the :class:`ListLine` sequence the
list draws — a conversation's heading, its members, a section over a sender's
mail, a plain row — and it is where every rule about grouping lives. Kept here
rather than in the state so the whole of it is checkable without a graph and
without instantiating Reflex; the state's computed var only hands it what it
holds. Which group a row sits in arrives as a :class:`Membership`, and the
five reads that produce one are :mod:`mailarc_ui.search.memberships`'s.
"""

from __future__ import annotations

import re
from collections.abc import Container, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from mailarc_analytics import GroupMembershipRow, TopicMembershipRow
from mailarc_analytics.semantic import SearchKind
from mailarc_core.archive.model import (
    Conversation,
    MessageSummary,
    Recipient,
    TagSummary,
)
from mailarc_core.archive.search import SearchFilters
from mailarc_ui.insights.model import short_key
from mailarc_ui.message_detail.model import NO_SUBJECT, LabelChip

MODE_FULLTEXT = SearchKind.FULLTEXT.value
MODE_SEMANTIC = SearchKind.SEMANTIC.value
"""The two ways of asking, named off the enum the archive answers under.

Spelled as the analytics component spells them rather than as two literals of
our own: the string reaches
:class:`~mailarc_analytics.semantic.model.SearchRequest` unchanged, and a
third path cannot appear in one place and not the other.
"""

class Grouping(StrEnum):
    """The positions of the **Group by** dropdown over the list.

    A ``StrEnum`` rather than eight literals, because the value crosses the
    socket and comes back as a string: :func:`grouping_of` is where a value
    nobody offered is narrowed to the default rather than trusted, and a
    handler comparing against a member cannot misspell one.
    """

    NONE = "none"
    CONVERSATION = "conversation"
    TOPIC = "topic"
    TAG = "tag"
    RECURRING = "recurring"
    SUBJECT = "subject"
    SENDER = "sender"
    RECEIVER = "receiver"


GROUPING_OPTIONS: list[dict[str, str]] = [
    {"label": "None", "value": Grouping.NONE.value},
    {"label": "Conversation / Thread", "value": Grouping.CONVERSATION.value},
    {"label": "Topic", "value": Grouping.TOPIC.value},
    {"label": "Tag", "value": Grouping.TAG.value},
    {"label": "Recurring group", "value": Grouping.RECURRING.value},
    {"label": "Subject", "value": Grouping.SUBJECT.value},
    {"label": "Sender", "value": Grouping.SENDER.value},
    {"label": "Receiver", "value": Grouping.RECEIVER.value},
]
"""What the dropdown offers, in the order it offers it."""

READ_GROUPINGS = frozenset(
    {
        Grouping.CONVERSATION,
        Grouping.TOPIC,
        Grouping.TAG,
        Grouping.RECURRING,
        Grouping.RECEIVER,
    }
)
"""The groupings that need a read beside the page.

Sender and subject are already on every row; the flat list needs nothing. The
other five ask the archive which thread, topic, tag, recurring group or
recipient each row belongs to — one statement per page, and none at all while
the list is grouped some other way.
"""

NO_GROUP = "none"
"""The id of the bucket a row lands in when the read did not file it.

One bucket per grouping — a message in no topic, a message wearing no tag —
and it sits where its first member sat like any other group. Its label is
:data:`UNFILED`'s.
"""

UNFILED: dict[Grouping, str] = {
    Grouping.TOPIC: "No topic",
    Grouping.TAG: "No tag",
    Grouping.RECURRING: "No group",
    Grouping.RECEIVER: "No recipient",
    Grouping.SENDER: "No sender",
    Grouping.SUBJECT: NO_SUBJECT,
}
"""What the bucket is called, per grouping."""

KEYWORDS_SHOWN = 3
"""How many of a topic's words name it when its members had no subject in
common."""


def grouping_of(value: str) -> Grouping:
    """The dropdown's value as the enum; anything nobody offered is the default.

    The same narrowing :func:`_attachment_filter` does for the attachment
    segment: a string arrives over the socket, and a caller that sends
    ``"everything"`` gets the conversations, not an exception.
    """
    try:
        return Grouping(value)
    except ValueError:
        return Grouping.CONVERSATION

ATTACH_ANY = "any"
ATTACH_WITH = "with"
ATTACH_WITHOUT = "without"
"""The attachment segment's three positions.

Three and not a checkbox, because
:attr:`~mailarc_core.archive.search.SearchFilters.has_attachments` is a
tri-state: "either" is not a filter, "without" is one, and a box that is
merely unticked cannot say which of the two it means.
"""

SEARCH_FAILED = (
    "The archive could not be searched right now. Its graph or the embedding "
    "service did not answer — check that the mail archive is running, then "
    "try again. The details are in the application log."
)
"""What a fault looks like on screen, and never the exception's own text.

The same refusal the MCP server makes, for the same reason: a driver's
message carries a path out of this installation
— an unreachable graph is ``Error 61 connecting to 127.0.0.1:6379``, a
missing blob names ``…/mailstore/ab/cd.eml`` — and this page renders whatever
it is given into a browser. Nothing is lost; the state logs the exception
with its traceback first.
"""

SEMANTIC_IS_TEXT_ONLY = (
    "Semantic search reads the question and nothing else — the sender, date, "
    "attachment and account fields do not narrow it."
)
"""Why half the form goes grey when the semantic segment is chosen.

Said rather than merely done. The fields are disabled *and* the sentence is
shown, because a form that quietly stops honouring what is typed in it is the
one way a search can lie about what it searched.
"""

_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
"""A run of letters or digits, in any script — the pieces of a name.

Written as "not a non-word character and not an underscore" so that *Rehpöhler*
and *Müller* keep their first letter, and so that ``first.last@firma.de`` reads
as two words rather than one.
"""

_TWO = 2
"""How many letters an avatar shows."""

_DATE_SHAPES = ("%d.%m.%Y",)
"""What a person types, after ISO has been tried and failed."""

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR
_WEEK = 7 * _DAY
_YEAR = 365 * _DAY
"""The steps a relative time is rounded down to, in seconds."""


class ResultRow(BaseModel):
    """One row of the result list — everything already printable.

    Frozen, like every row this archive renders: choosing one hands its id
    back to the state, which finds the row again and reads the original by
    the digest it carries.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    sender: str
    initials: str
    sender_address: str
    subject: str
    preview: str
    when_label: str
    """How long ago, the way a mail list says it — ``9m``, ``2h``, ``3d``."""

    has_attachments: bool = False
    attachment_count: int = 0
    """How many files the chip names, or ``0`` when the count is not known.

    The archive's summary answers *whether* a message carries attachments and
    not how many, so this is zero today and the chip shows the paperclip on
    its own. It is a field rather than a constant because the number is the
    thing the design asks for, and the row prints it the moment a reader
    hands one over.
    """

    labels: list[LabelChip] = []
    relevance_label: str = ""
    """``92%`` for a ranked hit, empty for a structural match.

    Empty and not ``0%``: a filtered listing matches or it does not, and
    printing a score for that would invent a ranking nobody computed.
    """

    eml_sha256: str = ""

    subject_norm: str = ""
    """The subject as the archive compares subjects — what grouping by subject
    groups on. Never printed; :attr:`subject` is what the row shows.

    Which group a row sits in is otherwise *not* on the row: the state holds a
    :class:`Membership` per message beside the rows, so a group's size and
    label live in one place rather than on each of its members.
    """

    @classmethod
    def from_summary(
        cls,
        summary: MessageSummary,
        now: datetime,
        relevance: float | None = None,
    ) -> ResultRow:
        """One summary as a row, ranked where the answer carried a ranking."""
        sender = summary.sender_name or summary.sender_address
        return cls(
            id=summary.id,
            sender=sender,
            initials=initials_of(sender),
            sender_address=summary.sender_address,
            subject=summary.subject or NO_SUBJECT,
            preview=summary.preview,
            when_label=relative_label(summary.sent_at, now),
            has_attachments=summary.has_attachments,
            labels=[LabelChip.from_label(one) for one in summary.labels],
            relevance_label=percent_label(relevance),
            eml_sha256=summary.eml_sha256 or "",
            subject_norm=summary.subject_norm,
        )


class Membership(BaseModel):
    """Which group one message sits in, under the grouping that was chosen.

    One shape for five reads, so :func:`lines_of` has one thing to look up.
    ``total`` is the group's *true* size and only a conversation knows one —
    the thread node counts its members; every other grouping's chip prints
    how many of the group this answer is showing. ``label`` is what a section
    header says, and is empty for a conversation because that heading is a
    message row and says the message.
    """

    model_config = ConfigDict(frozen=True)

    group_id: str
    label: str = ""
    total: int = 0

    @classmethod
    def of_conversation(cls, conversation: Conversation) -> Membership:
        return cls(group_id=conversation.id, total=conversation.total)

    @classmethod
    def of_recipient(cls, recipient: Recipient) -> Membership:
        return cls(
            group_id=recipient.address, label=recipient.name or recipient.address
        )

    @classmethod
    def of_tags(cls, tags: Sequence[TagSummary]) -> Membership | None:
        """The first tag by name — the tag store already sorts them so.

        One group per message, the way every grouping here files a row once:
        a message wearing two tags sits under the first, and appears under
        the other only when grouped by *that* tag's name — which is to say,
        never twice on one screen.
        """
        if not tags:
            return None
        first = tags[0]
        return cls(group_id=first.id, label=first.name or first.id)

    @classmethod
    def of_topic(cls, row: TopicMembershipRow) -> Membership:
        return cls(group_id=row.topic_id, label=topic_label(row))

    @classmethod
    def of_group(cls, row: GroupMembershipRow) -> Membership:
        return cls(group_id=row.group_id, label=group_label(row))


def topic_label(row: TopicMembershipRow) -> str:
    """What a topic's section is called: its subject, else its words, else its key.

    A topic's ``label`` is the subject its members had in common and is empty
    when they had none; the keywords are then the best name there is, and the
    digest's readable end is the name of last resort — the same
    :func:`~mailarc_ui.insights.model.short_key` the insights table prints.
    """
    if row.label:
        return row.label
    if row.keywords:
        return " · ".join(row.keywords[:KEYWORDS_SHOWN])
    return short_key(row.topic_id)


def group_label(row: GroupMembershipRow) -> str:
    """``5 people · 8f3a2c1d9e0b`` — a group has no name, only a size and a key."""
    return f"{row.size} people · {short_key(row.group_id)}"


class ListLine(BaseModel):
    """One line of the result list: a conversation's heading, or a message.

    Two shapes in one model rather than two models, because the list renders
    them into one scroll region and ``rx.foreach`` iterates one type. Which
    shape a line is, :attr:`is_header` says, and the component picks with an
    ``rx.cond``.

    **A heading is itself a message row.** It carries the top member's own
    sender, subject, preview and chips, plus a chevron and a size chip — so a
    collapsed conversation hides nothing a reader had already been shown, and a
    conversation of one message needs no special case: it is simply a row
    without a chevron. That is what makes clicking the heading mean "open the
    newest message" and clicking the chevron mean "show me the rest", which is
    how every mail client that groups already behaves.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    """Unique per line — ``c:<conversation>``, ``g:<group>`` or ``m:<message>``.

    ``rx.foreach`` needs a stable identity per line, and a message id alone
    will not do: the top member of a group appears as its heading *and* as a
    member once the group is open.
    """

    is_header: bool = False
    """A conversation's heading — a message row with a chevron in front."""

    is_section: bool = False
    """A section over a group that is not a conversation — a sender's mail, a
    topic's, a tag's. Not a message: nothing opens when it is clicked except
    the group itself, and :attr:`label` is all it says."""

    indented: bool = False
    """Whether this line sits under a heading. One CSS rule, not a style Var."""

    group_id: str = ""
    label: str = ""
    size_label: str = ""
    """``3 of 12`` — what the search returned, and how big the whole thing is;
    on a section, simply how many of the group this answer is showing."""

    can_expand: bool = False
    """Whether the archive holds members this answer did not return."""

    expanded: bool = False
    busy: bool = False
    """Whether *this* group's fetch is running. Never the list's own spinner."""

    id: str = ""
    sender: str = ""
    initials: str = ""
    subject: str = ""
    preview: str = ""
    when_label: str = ""
    has_attachments: bool = False
    attachment_count: int = 0
    labels: list[LabelChip] = []
    relevance_label: str = ""

    @classmethod
    def of(cls, row: ResultRow, **shape: object) -> ListLine:
        """One row as a line, plus whatever the grouping makes of it.

        ``dict(row)`` rather than a field-by-field copy: pydantic hands over
        the real :class:`LabelChip` objects, and a copy written out by hand is
        a copy that silently drops the next field somebody adds to a row.
        """
        fields = dict(row)
        fields.pop("sender_address", None)
        fields.pop("eml_sha256", None)
        fields.pop("subject_norm", None)
        return cls(**fields, **shape)  # type: ignore[arg-type]


class SearchAnswer(BaseModel):
    """One search's whole answer, ready to be applied under the state lock.

    The shape :class:`~mailarc_ui.dashboard.model.Readout` established: a
    background handler reads *outside* the lock and writes *inside* it, so
    what crosses that boundary is one frozen object rather than six
    assignments a future edit could leave half-applied.

    ``notice`` and ``error`` are the two ways an answer can be empty and they
    are not the same thing. A notice is a statement about the question or the
    configuration — "that has no searchable words in it", "only 40% of the
    archive is embedded" — and is shown as written. An error is a fault, and
    what is shown for one is :data:`SEARCH_FAILED`.
    """

    model_config = ConfigDict(frozen=True)

    rows: tuple[ResultRow, ...] = ()
    total: int = 0
    """How many messages match, or ``0`` when nothing counted them.

    A full-text answer is ranked and un-counted by design — counting it would
    mean running the procedure a second time without the page cut — so zero
    here means "not known", and the list asks for another page when the last
    one came back full rather than against a denominator.
    """

    memberships: dict[str, Membership] = {}
    """Which group each of this answer's rows sits in, keyed by message id.

    Empty for a grouping that needs no read — see
    :data:`READ_GROUPINGS` — because the read is then not made at all; the
    flat list costs exactly what it always cost. Carried on the answer rather
    than fetched separately so one frozen object still crosses the state lock.
    """

    grouping: str = ""
    """The grouping :attr:`memberships` were read for.

    A page is asked under one grouping and applied under whatever the dropdown
    says by the time it arrives; the state compares the two, so a switch made
    while a page was in flight cannot file that page's rows under the wrong
    kind of group.
    """

    notice: str = ""
    error: str = ""


def lines_of(
    rows: Sequence[ResultRow],
    memberships: Mapping[str, Membership],
    *,
    grouping: Grouping,
    collapsed: Container[str],
    whole: Mapping[str, Sequence[ResultRow]],
    busy: str,
) -> list[ListLine]:
    """The rows as the list draws them: headings, sections, members, plain rows.

    Pure, so the grouping is checkable without a graph, a socket or a Reflex
    state — this module's rule, and the reason the state's computed var is four
    lines long.

    Three shapes. Ungrouped is one line per row. A conversation is a heading
    that *is* its newest returned message, with its members under it — and a
    conversation of one, or a row in no conversation, is a plain row, because
    chrome around a group of one says nothing. Every other grouping is a
    section: a label, a count and a chevron over every member, groups of one
    included, because a row that does not itself say which topic or recipient
    it belongs to needs the section to say it.

    **A group sits where its first-seen member sat.** The rows arrive in answer
    order, so on the browse and structured paths that means "where its newest
    returned member sat", and on the ranked ones — full-text, semantic — "where
    its best hit sat". One rule, and it keeps a ranking readable: grouping
    never moves a good hit down the page, it only pulls that hit's siblings up
    under it.
    """
    if grouping == Grouping.NONE:
        return [ListLine.of(row, key=f"m:{row.id}") for row in rows]
    if grouping == Grouping.CONVERSATION:
        return _conversation_lines(
            rows, memberships, collapsed=collapsed, whole=whole, busy=busy
        )
    return _section_lines(rows, filed_by(rows, memberships, grouping), collapsed)


def filed_by(
    rows: Sequence[ResultRow],
    memberships: Mapping[str, Membership],
    grouping: Grouping,
) -> dict[str, Membership]:
    """Which group each row sits in, under one grouping — every row filed.

    Sender and subject are decided off the row itself and cost no read; the
    rest come out of ``memberships``, and a row the read did not file goes to
    the grouping's :data:`NO_GROUP` bucket rather than vanishing.
    """
    if grouping == Grouping.SENDER:
        return {
            row.id: Membership(
                group_id=row.sender_address or NO_GROUP,
                label=row.sender or UNFILED[grouping],
            )
            for row in rows
        }
    if grouping == Grouping.SUBJECT:
        return {
            row.id: Membership(
                group_id=row.subject_norm or NO_GROUP, label=row.subject
            )
            for row in rows
        }
    unfiled = Membership(group_id=NO_GROUP, label=UNFILED.get(grouping, ""))
    return {row.id: memberships.get(row.id, unfiled) for row in rows}


def _conversation_lines(
    rows: Sequence[ResultRow],
    memberships: Mapping[str, Membership],
    *,
    collapsed: Container[str],
    whole: Mapping[str, Sequence[ResultRow]],
    busy: str,
) -> list[ListLine]:
    """Headings that are messages, members under them, plain rows between."""
    members = _members(rows, memberships)
    lines: list[ListLine] = []
    for row in rows:
        found = memberships.get(row.id)
        if found is None or found.total <= 1:
            lines.append(ListLine.of(row, key=f"m:{row.id}"))
            continue
        if row.id != members[found.group_id][0].id:
            continue
        lines.extend(
            _group(
                members[found.group_id],
                found,
                whole=whole,
                collapsed=collapsed,
                busy=busy,
            )
        )
    return lines


def _section_lines(
    rows: Sequence[ResultRow],
    filed: Mapping[str, Membership],
    collapsed: Container[str],
) -> list[ListLine]:
    """A section per group, its members indented under it while it is open."""
    members = _members(rows, filed)
    lines: list[ListLine] = []
    drawn: set[str] = set()
    for row in rows:
        found = filed[row.id]
        if found.group_id in drawn:
            continue
        drawn.add(found.group_id)
        shown = members[found.group_id]
        open_now = found.group_id not in collapsed
        lines.append(
            ListLine(
                key=f"g:{found.group_id}",
                is_section=True,
                group_id=found.group_id,
                label=found.label,
                size_label=str(len(shown)),
                expanded=open_now,
            )
        )
        if open_now:
            lines.extend(
                ListLine.of(one, key=f"m:{one.id}", indented=True) for one in shown
            )
    return lines


def _members(
    rows: Sequence[ResultRow], memberships: Mapping[str, Membership]
) -> dict[str, list[ResultRow]]:
    """The rows of each group, in the order the answer returned them."""
    found: dict[str, list[ResultRow]] = {}
    for row in rows:
        membership = memberships.get(row.id)
        if membership is not None:
            found.setdefault(membership.group_id, []).append(row)
    return found


def _group(
    returned: Sequence[ResultRow],
    membership: Membership,
    *,
    whole: Mapping[str, Sequence[ResultRow]],
    collapsed: Container[str],
    busy: str,
) -> list[ListLine]:
    """One conversation: its heading, and its members when it is open."""
    shown = _shown(returned, whole.get(membership.group_id))
    head, rest = shown[0], shown[1:]
    open_now = membership.group_id not in collapsed
    heading = ListLine.of(
        head,
        key=f"c:{membership.group_id}",
        is_header=True,
        group_id=membership.group_id,
        size_label=size_label(len(shown), membership.total),
        can_expand=len(shown) < membership.total,
        expanded=open_now,
        busy=busy == membership.group_id,
    )
    if not open_now:
        return [heading]
    return [
        heading,
        *(ListLine.of(one, key=f"m:{one.id}", indented=True) for one in rest),
    ]


def _shown(
    returned: Sequence[ResultRow], fetched: Sequence[ResultRow] | None
) -> list[ResultRow]:
    """Which members this group draws — what was fetched wins, then the answer.

    A returned row that the fetch does not hold can only happen when the fetch
    hit its cap, and dropping it would take a search hit off the screen. So it
    is appended rather than replaced away, deduplicated by id.
    """
    if fetched is None:
        return list(returned)
    seen = {one.id for one in fetched}
    return [*fetched, *(one for one in returned if one.id not in seen)]


def size_label(shown: int, total: int) -> str:
    """What a conversation's chip prints — ``3 of 12``, or ``12`` when whole.

    The rule the count strip above the list already keeps: a denominator that
    equals the numerator says nothing twice.
    """
    return f"{shown} of {total}" if total > shown else f"{shown}"


def initials_of(name: str) -> str:
    """The one or two letters an avatar shows, handed over as two words.

    Two words, because Mantine derives an avatar's letters from the first and
    last *word* of the name it is given — ``"AB"`` would print as ``A``.
    Passing ``"A B"`` is what makes the archive's own rule the one that shows:
    the first letter of the first and last word of a display name, and the
    first two letters of the local part when a sender has no name at all, so
    ``shop@example.com`` gets two letters where the browser's own guess gives
    one.
    """
    local = name.partition("@")[0]
    words = _WORDS.findall(local)
    if not words:
        return ""
    if len(words) == 1:
        return " ".join(words[0][:_TWO]).upper()
    return f"{words[0][0]} {words[-1][0]}".upper()


def relative_label(sent_at: datetime | None, now: datetime) -> str:
    """How long ago a message arrived, as a mail list writes it.

    One unit, rounded down, largest that fits: ``9m``, ``2h``, ``3d``, ``5w``,
    ``2y``. A message from the last minute is ``now``, and so is one dated in
    the future — a ``Date:`` header is whatever a sender wrote, and *in 3d* is
    a claim this list has no business repeating.

    Both instants are made aware before they are subtracted, because either
    may be naive: the graph stores whatever the header carried, offset or not.
    """
    if sent_at is None:
        return ""
    seconds = (_aware(now) - _aware(sent_at)).total_seconds()
    if seconds < _MINUTE:
        return "now"
    for step, unit in ((_YEAR, "y"), (_WEEK, "w"), (_DAY, "d"), (_HOUR, "h")):
        if seconds >= step:
            return f"{int(seconds // step)}{unit}"
    return f"{int(seconds // _MINUTE)}m"


def percent_label(relevance: float | None) -> str:
    """A relevance as the chip prints it, or nothing at all.

    The scale is already ``0..1`` and already relative to one answer — both
    the full-text reader and the KNN normalise on the way out — so this only
    has to make it readable. ``None`` means the answer carried no ranking.
    """
    if relevance is None:
        return ""
    return f"{round(max(0.0, min(1.0, relevance)) * 100)}%"


def parse_date(value: str) -> datetime | None:
    """The day a date field names, or ``None`` when it names nothing.

    Two shapes, because two things write this string: Mantine's date input
    sends ISO, and a person typing into one sends what the field displays.
    Anything else filters nothing — a half-typed ``12.`` has to leave the
    result list alone rather than empty it.

    Naive on purpose. The bound is compared against ``sent_at`` as stored,
    which is wall-clock with whatever offset the header carried, so a picked
    day has to mean the same wall-clock day the rows show.
    """
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    for shape in _DATE_SHAPES:
        try:
            return datetime.strptime(text, shape)  # noqa: DTZ007 — naive on purpose
        except ValueError:
            continue
    return None


def filters_of(
    *,
    query: str = "",
    sender: str = "",
    recipient: str = "",
    date_from: str = "",
    date_to: str = "",
    attachments: str = ATTACH_ANY,
    account_id: str = "",
) -> SearchFilters:
    """One filled form as the archive reads it.

    The one place the browser's strings become a query, so the two decisions
    that would otherwise be spread over the form live here: an unrecognised
    attachment segment is "either" rather than an error, and the **To** date
    is stretched to the end of its day, because a person who picks the 30th
    means through the 30th and a bound at midnight would drop everything sent
    on it.
    """
    return SearchFilters(
        text=query.strip(),
        sender=sender.strip(),
        recipient=recipient.strip(),
        sent_from=parse_date(date_from),
        sent_until=_end_of_day(parse_date(date_to)),
        has_attachments=_attachment_filter(attachments),
        account_id=account_id.strip(),
    )


def _attachment_filter(segment: str) -> bool | None:
    """The segment as a tri-state; anything unknown asks for either."""
    if segment == ATTACH_WITH:
        return True
    if segment == ATTACH_WITHOUT:
        return False
    return None


def _end_of_day(moment: datetime | None) -> datetime | None:
    """The last instant of a picked day, so the day itself is included."""
    if moment is None:
        return None
    return moment.replace(hour=23, minute=59, second=59, microsecond=999999)


def _aware(moment: datetime) -> datetime:
    """The same instant, with UTC assumed where no zone was stored."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
