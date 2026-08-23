"""What a catalogue row looks like once a table is going to print it.

Every value object the insights page renders lives here, and not one of them
knows how to read anything. The split off :mod:`mailarc_ui.insights.state` is
worth a second module because the two halves are wrong in different ways: a
projection is wrong when a date comes out in the wrong zone or a verdict is
worded so a reader draws the opposite conclusion, and a state is wrong when a
lock is held across an await or a panel is left spinning over data that
already arrived. Checking the first needs no graph, no registry and no event
loop — a row in, a string out — and while both halves shared a file that was
true but impossible to see.

The projections are named ``…View`` rather than ``…Row`` for one reason:
``mailarc_analytics`` already exports a ``GroupRow``, a ``TopicRow`` and a
``TemplateRow``, and this module holds the other half of each of those pairs.
A reader looking at two neighbouring lines should not have to work out which
package a ``TopicRow`` came from.

The last two — :class:`HitView` and :class:`Found` — belong to the search
panel rather than to an analysis, and are here for the same reason as the
rest: what a hit *says* is decided without a graph. In particular
:func:`found_summary` is where a listing of twenty rows is stopped from
claiming to be a count of the archive.

The sentinels in the middle — :data:`NO_TOTALS` and the three beside it — are
what keeps ``None`` out of every component. "Nothing has been read yet" is an
empty view carrying the same fields rather than an absence each of five panels
would have to guard, which is the same reason
:func:`mailarc_core.graph.status.read_status` answers an unreachable server
with a status rather than an exception.
"""

import logging
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from mailarc_analytics import (
    ArchiveTotals,
    CoAddressedAgreement,
    CoAddressedRow,
    ComparedPair,
    GroupRow,
    TemplateRow,
    TopicRow,
    TopicSignal,
)
from mailarc_analytics.semantic import SearchHit, SearchKind, SearchResult
from mailarc_sync.jobs import JobState, SyncJob
from mailarc_ui.imports import percent_of

logger = logging.getLogger(__name__)

KEY_DIGITS = 12
"""How much of a derived key a table prints.

Every one of them is a digest — a ``Group`` is keyed by the sha256 of its
participants, a ``Topic`` by the digest of its membership — and none of them is
a name. Twelve hex characters tell two rows apart in any archive a desktop
holds and are still enough to find the node again by prefix.
"""

SAMPLE_LIMIT = 240
"""How much of a template's text one row shows, in characters.

Enough to recognise the boilerplate; the point of the row is the score and the
count beside it, not reading the mail again.
"""

DISPUTE_LIMIT = 20
"""How many disagreeing pairs the cross-check lists.

The check itself compares five hundred pairs a side — a verdict covers as much
of the archive as it can afford to — but a broken write path can disagree about
all of them, and a table of five hundred rows says no more than its first
twenty do. The count of what was left out is shown beside it.
"""

_NO_PERCENT = "—"
"""Stands in while a rebuild has not reported a stage yet."""

NO_SUBJECT = "(no subject)"
"""What a result row says instead of an empty cell.

A mail with no ``Subject`` is ordinary — automated notifications send them by
the thousand — and a blank cell in a list of subjects reads as a rendering
fault rather than as the message it is.
"""

_STATE_COLORS = {
    JobState.QUEUED: "gray",
    JobState.RUNNING: "blue",
    JobState.SUCCEEDED: "teal",
    JobState.FAILED: "red",
    JobState.CANCELLED: "orange",
}
"""What each job state should look like on this panel.

A second map beside the import panel's rather than a shared one: the vocabulary
belongs to ``JobState`` and is imported, the colours are a decision each panel
makes next to the words it prints. Sharing them would tie two panels' looks
together for no reason other than that both render a job.
"""

METHOD_COLORS = {
    TopicSignal.REF: "teal",
    TopicSignal.THREAD: "blue",
    TopicSignal.SUBJECT: "indigo",
    TopicSignal.ATTACHMENT: "yellow",
    TopicSignal.PARTICIPANTS: "orange",
}
"""How strong the signal behind a topic is, as a colour.

§6.2 makes the method the difference between a fact and a suggestion — two
messages naming the same ticket against two messages merely sent to the same
people — and a reader has to see which one they are looking at without
clicking. Cool for the signals that carry a topic on their own, warm for the
two that only carry one together.
"""


def method_color(method: str) -> str:
    """The colour a topic's signal is shown in; grey for one we do not know.

    ``ABOUT.method`` is a plain string and not the enum for exactly this case:
    a graph written by a build that knows one more signal still has to render
    here, and an unknown method is a topic to be sceptical of rather than one
    to hide.
    """
    try:
        return METHOD_COLORS[TopicSignal(method)]
    except ValueError:
        return "gray"


def short_key(value: str, digits: int = KEY_DIGITS) -> str:
    """The part of a derived id that tells two rows apart.

    Every key is a digest under a prefix that names its kind — ``topic:8f3…``,
    ``template:1a2b…:sent`` — and the prefix is already the table the row is
    in. The longest colon-separated part is the digest; what surrounds it is
    the kind and the direction, which the page says in words.
    """
    digest = max(value.split(":"), key=len) if value else ""
    return digest if len(digest) <= digits else f"{digest[:digits]}…"


def short_date(value: datetime | None) -> str:
    """One date the way a table prints one, in the reader's own zone.

    Total, because a ``Date:`` header is attacker-controlled and the archive
    range-checks nothing: ``Date: Fri, 31 Dec 9999 23:59:59 +0000`` parses,
    gets written to ``last_seen``, comes back an aware datetime — and then
    ``astimezone()`` raises ``OverflowError`` in every zone east of UTC,
    because the local wall-clock instant is past ``datetime.max``. Year 0001
    does the same west of it. One archived spam mail therefore used to raise
    out of the whole readout and leave all five panels spinning with nothing
    to say.

    An empty string rather than a clamped or a raw date: a date the table
    cannot print is one empty cell, which :func:`span_label` already treats as
    a legitimate answer. Fixed here and not at the decode boundary on purpose —
    ``rows.as_datetime`` decodes what the archive stores, and whether that
    instant can be *rendered* depends on the reader's own zone. A layer that
    answered differently in Berlin and in UTC for one archive would be the
    worse bug.
    """
    if value is None:
        return ""
    try:
        return value.astimezone().strftime("%d.%m.%y")
    except OverflowError, ValueError, OSError:
        logger.warning("Date outside the range this zone can print: %r", value)
        return ""


def span_label(first: datetime | None, last: datetime | None) -> str:
    """How long a finding has been alive, as one cell.

    A single date when both ends fall on the same day, and nothing at all when
    the graph could answer neither — an empty cell is honest where a dash would
    read like a value.
    """
    start, end = short_date(first), short_date(last)
    if not start or not end:
        return start or end
    return start if start == end else f"{start} – {end}"


def sample_label(text: str, limit: int = SAMPLE_LIMIT) -> str:
    """A template's text on one line, cut where a row stops caring.

    Whitespace is collapsed first: a cleaned body still carries the newlines it
    was written with, and a cell that honoured them would turn one row into
    thirty.
    """
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit].rstrip()}…"


def _plural(count: int, word: str) -> str:
    """``1 pair`` / ``4 pairs`` — the counts here are read as sentences."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _count_label(value: int | None) -> str:
    """One side's count, or the dash that means it never named the pair.

    ``None`` is weaker than zero and has to look different from it: on a
    listing that was cut it means "not in the top rows", not "never happened".
    """
    return _NO_PERCENT if value is None else str(value)


class TotalsView(BaseModel):
    """What the archive holds and what has been derived from it.

    Frozen, like every projection here: a reading, never a handle.
    """

    model_config = ConfigDict(frozen=True)

    messages: int = 0
    unidentified: int = 0
    groups: int = 0
    topics: int = 0
    templates: int = 0
    co_addressed: int = 0

    derived: int = 0
    """Everything a rebuild wrote, added up.

    A stored field and not the property it comes from, because a property is
    not a pydantic field and Reflex serialises fields: what a component can
    read is what was written down here.
    """

    @classmethod
    def from_totals(cls, totals: ArchiveTotals) -> TotalsView:
        return cls(**totals.model_dump(), derived=totals.derived)


class PairView(BaseModel):
    """One co-addressed pair as the materialised edge carries it."""

    model_config = ConfigDict(frozen=True)

    left_id: str = ""
    right_id: str = ""
    together: int = 0
    span: str = ""

    @classmethod
    def from_row(cls, row: CoAddressedRow) -> PairView:
        return cls(
            left_id=row.left_id,
            right_id=row.right_id,
            together=row.together,
            span=span_label(row.first_seen, row.last_seen),
        )


class DisputeView(BaseModel):
    """One pair the two readings of A1 do not tell the same story about."""

    model_config = ConfigDict(frozen=True)

    left_id: str = ""
    right_id: str = ""
    truth: str = _NO_PERCENT
    """What the self-join over ``SENT_TO``/``COPIED_TO`` counted."""

    edge: str = _NO_PERCENT
    """What ``CO_ADDRESSED`` carries."""

    note: str = ""
    """What the difference means, in the shortest wording that is still true."""

    note_color: str = "yellow"
    overstated: bool = False
    """Whether the edge is the side claiming more.

    The only direction with no innocent reading, which is why it is a flag on
    the row and not something a reader has to work out from two numbers.
    """

    @classmethod
    def from_pair(
        cls, pair: ComparedPair, *, overstated: bool, truth_floor: int = 0
    ) -> DisputeView:
        return cls(
            left_id=pair.left_id,
            right_id=pair.right_id,
            truth=_count_label(pair.truth),
            edge=_count_label(pair.edge),
            note=_dispute_note(pair, overstated=overstated, truth_floor=truth_floor),
            note_color="red" if overstated else "yellow",
            overstated=overstated,
        )


def _dispute_note(pair: ComparedPair, *, overstated: bool, truth_floor: int) -> str:
    """The sentence beside one disagreement, held to what was actually proved.

    The edge-only case is the one that needs care. What the comparison
    establishes is that the edge stands above the truth listing's floor, which
    proves the edge overstates — not that the archive has never seen the pair.
    Those are the same claim only when the floor is zero, i.e. when the truth
    listing came back short of *limit* and is therefore exhaustive. Above a
    cut, the pair may simply sit below it, and a reader who greps the archive
    and finds it has caught the panel saying something untrue.
    """
    if pair.truth is None:
        if truth_floor <= 0:
            return "the archive has no such pair"
        return f"the archive counts this pair at most {truth_floor}"
    if pair.edge is None:
        return "the edge never wrote this pair"
    return (
        "the edge counts more than the archive"
        if overstated
        else "the edge is behind the archive"
    )


class AgreementView(BaseModel):
    """A1's definition and A1's materialisation, judged against each other.

    The panel this feeds is the reason the page exists. Everything else here
    reports what an analysis found; this says whether the analysis can be
    believed at all, and it says it in the one direction that matters — an edge
    that counts *less* than the archive is behind it, an edge that counts
    *more* is claiming something no message supports.
    """

    model_config = ConfigDict(frozen=True)

    agrees: bool = False
    """The strict reading, straight off the verdict: nothing it ruled on
    differed."""

    color: str = "gray"
    headline: str = ""
    detail: str = ""
    coverage: str = ""
    """How much of the archive the verdict covers. Next to the verdict either
    way, because agreement is worth exactly as much as what was compared."""

    limit: int = 0
    compared: int = 0
    matched: int = 0
    mismatched: int = 0
    edge_only: int = 0
    truth_only: int = 0
    unjudged: int = 0
    overstated: int = 0
    """Disagreements where the edge is the side claiming more."""

    duplicate_pairs: int = 0
    """Pairs the edge listing named twice — one pair, two relationships."""

    disputes: list[DisputeView] = []
    """The heaviest disagreements, the overstating ones first."""

    disputes_total: int = 0
    """How many there were before :data:`DISPUTE_LIMIT` cut the listing."""

    disputes_note: str = ""
    """The line under that table: how much of it is shown, and how to read a
    dash. Written here rather than in the component because a Reflex component
    cannot interpolate a var into a sentence."""

    @classmethod
    def from_agreement(cls, found: CoAddressedAgreement) -> AgreementView:
        """Turn the verdict into the handful of strings a panel prints.

        The overstating pairs are put first and kept first: they are the only
        ones a reader has to act on, and a table that sorted purely by weight
        would bury one behind twenty pairs a stale rebuild explains.
        """
        overstated = found.edge_overstates
        loud = {(one.left_id, one.right_id) for one in overstated}
        quiet = sorted(
            (
                one
                for one in (*found.count_mismatches, *found.truth_only)
                if (one.left_id, one.right_id) not in loud
            ),
            key=lambda one: -one.heaviest,
        )
        color, headline, detail = _verdict(found)
        ordered = [*overstated, *quiet]
        shown = ordered[:DISPUTE_LIMIT]
        return cls(
            agrees=found.agrees,
            color=color,
            headline=headline,
            detail=detail,
            coverage=_coverage(found),
            limit=found.limit,
            compared=found.compared,
            matched=len(found.matched),
            mismatched=len(found.count_mismatches),
            edge_only=len(found.edge_only),
            truth_only=len(found.truth_only),
            unjudged=found.unjudged,
            overstated=len(overstated),
            duplicate_pairs=found.duplicate_pairs,
            disputes=[
                DisputeView.from_pair(
                    one,
                    overstated=index < len(overstated),
                    truth_floor=found.truth_floor,
                )
                for index, one in enumerate(shown)
            ],
            disputes_total=len(ordered),
            disputes_note=(
                f"Showing the heaviest {len(shown)} of {len(ordered)}. A dash "
                "means that side never named the pair at all."
            ),
        )


def _verdict(found: CoAddressedAgreement) -> tuple[str, str, str]:
    """The colour, the headline and the sentence under it.

    Three readings, told apart by the *direction* of the disagreement. An edge
    counting more than the self-join claims something no message supports, and
    none of the innocent explanations produces one — they all make a rebuild
    see less of the archive, never more. An edge counting less is what a stale,
    capped or wide-recipient rebuild looks like: worth printing, not worth
    alarming about. Colouring both red would spend the alarm on the case that
    happens every day and leave nothing for the case that must never happen.
    """
    if found.duplicate_pairs:
        return (
            "red",
            f"{_plural(found.duplicate_pairs, 'pair')} in the graph "
            "with two CO_ADDRESSED edges",
            "A rebuild's undirected MERGE updates the edge a pair already has "
            "rather than adding a second, so nothing the writer does produces "
            "this. Whatever the two counts say, the graph holds a row the "
            "write path cannot account for. Look at the A1 write path.",
        )
    overstated = len(found.edge_overstates)
    if overstated:
        return (
            "red",
            "The edge counts more than the archive supports on "
            f"{_plural(overstated, 'pair')}",
            "Nothing legitimate makes the materialised edge see more than the "
            "ground truth — a stale, capped or wide-recipient rebuild all make "
            "it see less. Look at the A1 write path.",
        )
    if found.agrees:
        return (
            "teal",
            f"The edge and the archive agree on all {found.compared} pairs the "
            "check could rule on",
            "CO_ADDRESSED still says what SENT_TO and COPIED_TO say.",
        )
    behind = len(found.count_mismatches) + len(found.truth_only)
    return (
        "yellow",
        f"The archive counts more than the edge on {_plural(behind, 'pair')}",
        "The edge is behind the archive rather than wrong about it: no rebuild "
        "since the last import, a rebuild under a message ceiling, or a mail "
        "addressed to more people than a pair is written for all read like "
        "this.",
    )


def _coverage(found: CoAddressedAgreement) -> str:
    """What the verdict covered, so nobody reads it as covering everything."""
    return (
        f"{found.compared} pairs judged, {found.unjudged} left open — each side "
        f"was asked for {found.limit} rows, and a pair below the other side's "
        "cut proves nothing either way."
    )


class GroupView(BaseModel):
    """A circle of people that keeps being written to."""

    model_config = ConfigDict(frozen=True)

    key: str = ""
    size: int = 0
    message_count: int = 0
    span: str = ""

    @classmethod
    def from_row(cls, row: GroupRow) -> GroupView:
        return cls(
            key=short_key(row.id),
            size=row.size,
            message_count=row.message_count,
            span=span_label(row.first_seen, row.last_seen),
        )


class TopicView(BaseModel):
    """One topic, and the signal that drew its edges.

    A topic appears once per method, never folded together: the method is the
    column a reader has to look at before believing the row.
    """

    model_config = ConfigDict(frozen=True)

    key: str = ""
    label: str = ""
    method: str = ""
    method_color: str = "gray"
    messages: int = 0

    @classmethod
    def from_row(cls, row: TopicRow) -> TopicView:
        return cls(
            key=short_key(row.id),
            label=row.label or "(no subject in common)",
            method=row.method or "unknown",
            method_color=method_color(row.method),
            messages=row.messages,
        )


class TemplateView(BaseModel):
    """A text that gets written again and again."""

    model_config = ConfigDict(frozen=True)

    key: str = ""
    occurrences: int = 0

    score: float = 0.0
    """The automation score as a bar takes it, ``0..100``."""

    score_label: str = "0.00"
    """The same number as it is defined, ``0..1`` — comparable within one
    direction and meaningless across them, so the bar never stands alone."""

    sample: str = ""
    span: str = ""

    @classmethod
    def from_row(cls, row: TemplateRow) -> TemplateView:
        return cls(
            key=short_key(row.id),
            occurrences=row.occurrences,
            score=round(100.0 * row.automation_score, 1),
            score_label=f"{row.automation_score:.2f}",
            sample=sample_label(row.sample_text),
            span=span_label(row.first_seen, row.last_seen),
        )


class RebuildJobView(BaseModel):
    """The rebuild job as the control shows it.

    A derive job counts *stages*, not messages: the worker moves the row on by
    one every time an analysis finishes, so ``3 / 7`` is three stages done and
    the percentage is a percentage of the work, not of the archive.
    """

    model_config = ConfigDict(frozen=True)

    job_id: int = 0
    status: str = ""
    status_color: str = "gray"
    percent: float = 0.0
    percent_label: str = _NO_PERCENT
    stages_label: str = ""
    error: str = ""
    active: bool = False
    cancel_requested: bool = False

    @classmethod
    def from_job(cls, job: SyncJob) -> RebuildJobView:
        percent = percent_of(job.progress)
        total = job.progress.total
        return cls(
            job_id=job.id,
            status=str(job.state),
            status_color=_STATE_COLORS.get(job.state, "gray"),
            percent=percent,
            percent_label=_NO_PERCENT if total <= 0 else f"{percent:.0f}%",
            stages_label=(
                "" if total <= 0 else f"{job.progress.done} of {total} stages"
            ),
            error=job.error or "",
            active=job.state in (JobState.QUEUED, JobState.RUNNING),
            cancel_requested=job.cancel_requested,
        )


NO_TOTALS = TotalsView()
NO_AGREEMENT = AgreementView()
NO_VERDICT = CoAddressedAgreement()
NO_JOB = RebuildJobView()
"""Nothing read yet. Sentinels keep every component free of `None`."""


class Readout(BaseModel):
    """Everything one refresh read, before any of it is a state var.

    A refresh happens in two places — the page's ``on_load`` and the poll that
    watches a rebuild — and only the second holds the state lock. Reading into
    one value and assigning it in one step is what lets both share the code:
    the lock goes around :meth:`AnalyticsInsightsState._apply` and around
    nothing that awaits. It also means a page never shows one analysis from
    before a rebuild beside another from after it.
    """

    model_config = ConfigDict(frozen=True)

    totals: TotalsView = NO_TOTALS
    totals_error: str = ""
    agreement: AgreementView = NO_AGREEMENT
    pairs: list[PairView] = []
    agreement_error: str = ""
    groups: list[GroupView] = []
    groups_error: str = ""
    topics: list[TopicView] = []
    topics_error: str = ""
    sent: list[TemplateView] = []
    received: list[TemplateView] = []
    templates_error: str = ""


class HitView(BaseModel):
    """One message a search found, as a result row prints it.

    No body and no bytes, for the reason
    :class:`~mailarc_analytics.semantic.model.SearchHit` carries none: a
    listing of twenty subjects that dragged twenty bodies through the socket
    would move megabytes to render four columns, and the review page is one
    click away with the original.

    ``score`` is deliberately two fields. The bar takes ``0..100`` and the
    label prints the number the search actually produced, because the two
    paths measure different things — a relevance ranked within one full-text
    answer, a cosine similarity against one query vector — and a bar alone
    would invite a reader to compare the two.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str = ""
    """The canonical id: what the review page, the MCP tools and every other
    read in this project key a message on."""

    subject: str = NO_SUBJECT
    sender: str = ""
    when: str = ""
    score: float = 0.0
    score_label: str = "0.00"

    @classmethod
    def from_hit(cls, hit: SearchHit) -> HitView:
        return cls(
            message_id=hit.message_id,
            subject=hit.subject or NO_SUBJECT,
            sender=hit.sender,
            when=short_date(hit.sent_at),
            score=round(100.0 * hit.score, 1),
            score_label=f"{hit.score:.2f}",
        )


class Found(BaseModel):
    """What one search came back with, before any of it is a state var.

    The same shape and the same reason as :class:`Readout`: a search is read
    off the state lock and applied under it in one step, so a panel can never
    show the hits of one query beside the sentence of another.

    ``error`` carries its own colour rather than being red by definition.
    "The graph did not answer" and "no embedder is configured" are both
    reasons there are no rows, and only the first is a fault — colouring the
    second red would tell a user with a perfectly healthy archive that
    something broke, when what they are reading is a setting they have not
    set.
    """

    model_config = ConfigDict(frozen=True)

    hits: list[HitView] = []
    summary: str = ""
    """What the rows above amount to, in one line. Written here rather than in
    the component for the reason :attr:`AgreementView.disputes_note` is: a
    Reflex component cannot interpolate a var into a sentence."""

    notice: str = ""
    """How much of the archive the search could see — empty unless something
    is missing. Straight off
    :attr:`~mailarc_analytics.semantic.model.SearchResult.notice`, because a
    KNN over a half-embedded archive answers short and looks complete."""

    error: str = ""
    error_color: str = "red"

    @classmethod
    def from_result(cls, result: SearchResult, *, asked: str, limit: int) -> Found:
        hits = [HitView.from_hit(one) for one in result.hits]
        return cls(
            hits=hits,
            summary=found_summary(
                len(hits), asked=asked, limit=limit, kind=result.kind
            ),
            notice=result.notice,
        )

    @classmethod
    def failed(cls, message: str, *, color: str = "red") -> Found:
        """No rows, and the sentence that says why — never an empty listing.

        The whole point of the phase's rule, at the last place it can still be
        broken: a caller that turned an unavailable search into ``Found()``
        would render "nothing matched", which is a claim about the archive
        rather than about the configuration.
        """
        return cls(error=message, error_color=color)


def found_summary(count: int, *, asked: str, limit: int, kind: SearchKind) -> str:
    """The line under a result table, held to what the search actually proved.

    A full result set is the case that needs care. A search asks for *limit*
    rows and the store obliges, so a page that read "37 messages match" off a
    listing of twenty would be inventing a total nobody counted — and a
    listing that came back *short* of the limit really is everything there is.
    The two are worded differently for that reason alone.

    The order is named because the two paths rank on different things and
    neither ranking is obvious from the numbers: full text ranks by relevance
    within this one answer, a KNN by distance from the query's vector.
    """
    order = "closest first" if kind is SearchKind.SEMANTIC else "best match first"
    if count <= 0:
        return f"Nothing in the archive matches “{asked}”."
    if count >= limit:
        return (
            f"The {count} strongest matches for “{asked}”, {order} — there may be more."
        )
    return f"{_plural(count, 'message')} for “{asked}”, {order}."
