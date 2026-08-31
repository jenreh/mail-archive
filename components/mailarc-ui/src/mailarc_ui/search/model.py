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
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from mailarc_analytics.semantic import SearchKind
from mailarc_core.archive.model import MessageSummary
from mailarc_core.archive.search import SearchFilters
from mailarc_ui.message_detail.model import NO_SUBJECT, LabelChip

MODE_FULLTEXT = SearchKind.FULLTEXT.value
MODE_SEMANTIC = SearchKind.SEMANTIC.value
"""The two ways of asking, named off the enum the archive answers under.

Spelled as the analytics component spells them rather than as two literals of
our own: the string reaches
:class:`~mailarc_analytics.semantic.model.SearchRequest` unchanged, and a
third path cannot appear in one place and not the other.
"""

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

The same refusal :data:`~mailarc_ui.insights.search.SEARCH_FAILED` makes, for
the same reason: a driver's message carries a path out of this installation
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
        )


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

    notice: str = ""
    error: str = ""


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
