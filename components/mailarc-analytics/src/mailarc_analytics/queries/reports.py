"""Asking the archive what the analyses found — the read façade.

:class:`~mailarc_core.archive.reader.ArchiveReader`'s counterpart, one layer
up: that one answers what the import wrote, this one answers what a rebuild
made of it. Same construction — a session factory and nothing else, because a
read is complete in itself and the caller that wants a listing should not also
have to know how a graph is opened — and the same projection discipline, so
what comes back are frozen value objects that outlive the session they were
read in.

**A caller cannot hand this class Cypher.** Every method below takes numbers
and a direction; the statements are named constants reached as
``catalog.SOMETHING`` and there is not a string of Cypher in this file. That
is the catalogue's whole rule (nothing outside it composes a statement) applied
to the one module whose job is to run statements on behalf of a page — and
phase 6 serves a model from the same constants for the same reason, so the
guarantee has to hold structurally rather than by care.

Synchronous, because every runic driver blocks. An async caller wraps a call in
``asyncio.to_thread`` the way the graph status reader does.

One session per question, not per statement: :meth:`totals` asks six counts and
:meth:`co_addressed_agreement` asks two listings, and opening a driver per
statement would pay six connections for one number and — worse for the
cross-check — read the two halves of a comparison from measurably different
moments.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from runic.ogm import Session

from mailarc_analytics.derived.model import TemplateDirection
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.model import (
    ArchivedDay,
    ArchiveTotals,
    CoAddressedAgreement,
    CoAddressedRow,
    CommunityRow,
    CoRecipientRow,
    GroupMembershipRow,
    GroupRow,
    ImportantMessageRow,
    TagSuggestionRow,
    TemplateRow,
    TopicKeywordsRow,
    TopicMembershipRow,
    TopicRow,
)
from mailarc_analytics.queries.rows import (
    as_datetime,
    as_float,
    as_int,
    as_text,
    rows_of,
)
from mailarc_core.archive.reader import GraphSessionFactory

logger = logging.getLogger(__name__)

REPORT_LIMIT = 50
"""Rows a listing returns unless the caller says otherwise.

A screenful and a bit. Every one of these statements is ordered by the number
that matters, so the interesting rows are the first ones and a bigger default
would only cost a page more to scroll.
"""

MAX_ROWS = 5_000
"""The most rows any caller can ask a report for.

An order of magnitude above :data:`AGREEMENT_LIMIT`, so nothing this repository
asks for is affected and no plausible reading of an archive is refused — it is
there to stop a report turning into an archive dump, not to second-guess a
caller who wants a big one.
"""

AGREEMENT_LIMIT = 500
"""Rows each side of the cross-check is asked for.

Ten times the listings, because the number means something different here.
Nobody reads these rows one by one — they are compared — and a pair only gets a
verdict when it stands above where the *other* listing was cut (see
:meth:`CoAddressedAgreement.between
<mailarc_analytics.queries.model.CoAddressedAgreement.between>`). So the limit
is not how much a human sees, it is how much of the archive the verdict covers,
and it is set as high as one self-join answer can be read in without holding an
archive's worth of pairs in memory.
"""


class AnalyticsReader:
    """The derived layer as a page needs it.

    Eleven listings, six counts and one verdict.

    Read-only, unlike its counterpart in ``mailarc-core``. Nothing here writes,
    because everything it reads is written by ``rebuild-derived`` and a report
    that could edit a finding would be editing an answer rather than the
    question that produced it.
    """

    def __init__(self, graph_session: GraphSessionFactory) -> None:
        self._graph_session = graph_session

    def totals(self) -> ArchiveTotals:
        """What the archive holds and what has been derived from it.

        Six statements in one session, so the six numbers describe one moment.
        Read together rather than one at a time because the only useful reading
        is the ratio between them: a hundred templates over thirty messages is
        a calibration failure that neither number shows on its own.
        """
        with self._graph_session() as graph:
            return ArchiveTotals(
                messages=_count(graph, catalog.COUNT_MESSAGES),
                unidentified=_count(graph, catalog.COUNT_UNIDENTIFIED),
                groups=_count(graph, catalog.COUNT_GROUPS),
                topics=_count(graph, catalog.COUNT_TOPICS),
                templates=_count(graph, catalog.COUNT_TEMPLATES),
                co_addressed=_count(graph, catalog.COUNT_CO_ADDRESSED),
            )

    def co_recipients(self, *, limit: int = REPORT_LIMIT) -> tuple[CoRecipientRow, ...]:
        """A1 computed off the ground truth, heaviest pair first.

        The definition, not the stored answer. It runs a self-join over every
        message and gets expensive on a large archive — which is exactly why
        the edge exists — so a page that only wants the ranking should ask
        :meth:`top_co_addressed` instead and keep this one for the comparison.
        """
        with self._graph_session() as graph:
            return _co_recipients(graph, _limit(limit))

    def top_co_addressed(
        self, *, limit: int = REPORT_LIMIT
    ) -> tuple[CoAddressedRow, ...]:
        """The same ranking read off the materialised ``CO_ADDRESSED`` edge."""
        with self._graph_session() as graph:
            return _co_addressed(graph, _limit(limit))

    def co_addressed_agreement(
        self, *, limit: int = AGREEMENT_LIMIT
    ) -> CoAddressedAgreement:
        """Run A1's definition and A1's materialisation against each other.

        The catalogue puts it plainly: if the edge and the self-join ever
        disagree, the edge is wrong. So the two statements are run back to back
        in **one** session — as close to one instant as a store without
        multi-statement transactions gets — and
        :meth:`CoAddressedAgreement.between
        <mailarc_analytics.queries.model.CoAddressedAgreement.between>` decides
        what the difference between two truncated top-N listings is allowed to
        prove.

        A truth-side excess is logged as ordinary news: a rebuild that has not
        run since the last import produces one, and so does any archive holding
        a mail to a big distribution list. An edge claiming more than the
        ground truth supports is logged as a warning, because nothing
        legitimate produces one.
        """
        asked = _limit(limit)
        with self._graph_session() as graph:
            truth = _co_recipients(graph, asked)
            edge = _co_addressed(graph, asked)
        found = CoAddressedAgreement.between(truth, edge, limit=asked)
        if found.edge_overstates:
            logger.warning(
                "CO_ADDRESSED claims %d pairs the ground truth does not support",
                len(found.edge_overstates),
            )
        else:
            logger.info(
                "Cross-checked %d co-addressed pairs (%d unjudged): %d disagreements",
                found.compared,
                found.unjudged,
                found.compared - len(found.matched),
            )
        return found

    def recurring_groups(
        self,
        *,
        min_size: int = 0,
        min_messages: int = 0,
        limit: int = REPORT_LIMIT,
    ) -> tuple[GroupRow, ...]:
        """Circles that keep being written to, busiest first.

        Both thresholds default to zero, meaning every ``Group`` the rebuild
        kept. The numbers that decided which groups *exist* were applied when
        they were written — a group below
        :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.min_group_size`
        was never a node — so asking for more here narrows a listing and
        cannot widen one. Defaulting to the configured thresholds instead would
        be a second copy of them, and the copy is the one that drifts.
        """
        with self._graph_session() as graph:
            rows = rows_of(
                graph,
                catalog.RECURRING_GROUPS,
                {
                    "min_size": min_size,
                    "min_messages": min_messages,
                    "limit": _limit(limit),
                },
            )
        return tuple(
            GroupRow(
                id=as_text(row["id"]),
                size=as_int(row["size"]),
                message_count=as_int(row["message_count"]),
                first_seen=as_datetime(row["first_seen"]),
                last_seen=as_datetime(row["last_seen"]),
            )
            for row in rows
        )

    def topics(self, *, limit: int = REPORT_LIMIT) -> tuple[TopicRow, ...]:
        """Topics by size, one row per signal that drew the edges.

        A topic holding messages joined by a ticket token and messages joined
        by a shared attachment comes back twice, and that is the point: the
        method is what separates a fact from a suggestion, and a page that
        summed the rows would be hiding the column a reader has to look at
        before believing any of them.
        """
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.TOPIC_BREAKDOWN, {"limit": _limit(limit)})
        return tuple(
            TopicRow(
                id=as_text(row["id"]),
                label=as_text(row["label"]),
                method=as_text(row["method"]),
                messages=as_int(row["messages"]),
            )
            for row in rows
        )

    def archived_per_day(self, *, days: int) -> tuple[ArchivedDay, ...]:
        """How the archive grew over the last *days* days, one row per day.

        Exactly *days* rows, oldest first, ending on today — a chart's x-axis
        is a calendar and not a list of the days something happened. The
        statement answers only about days that have copies on them, so the two
        things this method does to its answer are the two things a calendar
        needs: **cut to the window** and **fill the gaps with zeros**. A hole
        in a series reads as missing data, which is the opposite of what a
        quiet week means.

        **The window is measured in UTC days**, and it is the archive's own
        clock rather than a choice made here.
        :class:`~mailarc_core.archive.writer.MessageArchiver` stamps
        ``datetime.now(UTC)`` on every ``ARCHIVED_FROM`` edge whose source did
        not carry a time, and nothing in this repository carries one — a sync
        run always archives in the present — so every stored stamp is UTC and
        :data:`~mailarc_analytics.queries.catalog.ARCHIVED_PER_DAY` cuts a UTC
        date out of it. Anchoring the window on a local *today* would leave the
        newest column short or empty for anybody east of Greenwich, for as long
        as their day was ahead of the archive's.

        *days* goes through :func:`_limit`, and the clamped number is the
        window. **The row ceiling is deliberately wider than it**, which is the
        one place these two numbers must not be the same. The statement orders
        by day descending and stops at its ceiling, and not every row it can
        return belongs to a day inside the window: ``archived_at`` is a wall
        clock somebody else set, so a restored backup or a machine that ran
        ahead leaves day-rows *after* today, and those are returned first. Bound
        to the window itself, the ceiling was then spent on days nobody asked
        for and the oldest real ones were gap-filled as zeros — a chart saying
        the archive took nothing in on days it was taking mail in.
        :func:`_ceiling` is the room that leaves. It cannot buy room above
        ``MAX_ROWS``, where the window has already been clamped to the same
        number, and it does not need to: a window that wide is longer than any
        clock skew worth modelling.

        A day key that does not parse costs that one day and nothing else,
        the way :func:`~mailarc_analytics.queries.rows.as_datetime` treats an
        unreadable timestamp: ``left()`` cuts ten characters off whatever the
        property holds, so a stamp written by something other than the writer
        comes back as a key no calendar has — and a report that died over one
        of them would lose the whole chart.
        """
        window = _limit(days)
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.ARCHIVED_PER_DAY, {"limit": _ceiling(window)})
        counted = {
            found: ArchivedDay(
                day=found.isoformat(),
                messages=as_int(row["messages"]),
                bytes=as_int(row["bytes"]),
            )
            for row in rows
            if (found := _as_day(row["day"])) is not None
        }
        last = _today()
        return tuple(
            counted.get(one, ArchivedDay(day=one.isoformat()))
            for one in _span(last - timedelta(days=window - 1), last)
        )

    def templates(
        self, direction: TemplateDirection, *, limit: int = REPORT_LIMIT
    ) -> tuple[TemplateRow, ...]:
        """What is worth automating in one direction, best candidate first.

        One direction per call and never both, because §6.3 asks for them
        apart: only what you write yourself is automatable, and the scores are
        calibrated within a direction and mean nothing across one. The
        direction is bound as its ``str`` value rather than as the enum, and
        the reason moved with the statements. It used to be that a raw
        statement's parameters reached the driver unconverted. It is now that a
        *bound parameter* is not converted either: ``Template.direction`` is a
        mapped field, but the field's converter runs on the way into a node
        property, not over a value handed to ``bind()`` — measured. An enum
        member happens to work anyway, because
        :class:`~mailarc_analytics.derived.model.TemplateDirection` is a
        ``StrEnum`` and the member *is* the string, which is exactly why
        passing it would prove nothing and the ``.value`` stays explicit.
        """
        with self._graph_session() as graph:
            rows = rows_of(
                graph,
                catalog.TOP_TEMPLATES,
                {"direction": direction.value, "limit": _limit(limit)},
            )
        return tuple(
            TemplateRow(
                id=as_text(row["id"]),
                direction=direction,
                occurrences=as_int(row["occurrences"]),
                automation_score=as_float(row["automation_score"]),
                sample_text=as_text(row["sample_text"]),
                first_seen=as_datetime(row["first_seen"]),
                last_seen=as_datetime(row["last_seen"]),
            )
            for row in rows
        )

    def communities(self, *, limit: int = REPORT_LIMIT) -> tuple[CommunityRow, ...]:
        """Circles of correspondents, busiest first — B3's answer as a listing.

        Ordered by the mail that circulates in a circle rather than by how many
        people are in it, which is :meth:`recurring_groups`' choice and the
        same one: a circle of forty who exchanged three mails is a directory,
        and a circle of five who exchanged four hundred is where the work is.
        """
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.TOP_COMMUNITIES, {"limit": _limit(limit)})
        return tuple(
            CommunityRow(
                id=as_text(row["id"]),
                label=as_text(row["label"]),
                size=as_int(row["size"]),
                message_count=as_int(row["message_count"]),
                method=as_text(row["method"]),
                first_seen=as_datetime(row["first_seen"]),
                last_seen=as_datetime(row["last_seen"]),
            )
            for row in rows
        )

    def important_messages(
        self, *, limit: int = REPORT_LIMIT
    ) -> tuple[ImportantMessageRow, ...]:
        """What probably matters, best first — B2's answer, with its reasons.

        The statement filters ``importance IS NOT NULL``, so an archive nobody
        has run a rebuild over answers with nothing rather than with whichever
        unscored messages the store happened to visit.
        """
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.TOP_IMPORTANT, {"limit": _limit(limit)})
        return tuple(
            ImportantMessageRow(
                id=as_text(row["id"]),
                subject=as_text(row["subject"]),
                sent_at=as_datetime(row["sent_at"]),
                sender=as_text(row["sender"]),
                importance=as_float(row["importance"]),
                reasons=_as_words(row["reasons"]),
            )
            for row in rows
        )

    def topic_keywords(
        self, *, limit: int = REPORT_LIMIT
    ) -> tuple[TopicKeywordsRow, ...]:
        """What each topic is about, in its members' own words.

        One row per topic, unlike :meth:`topics` — that one is per topic *per
        signal*, and these words would come back once for every way the
        topic's messages were joined.
        """
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.TOPIC_KEYWORDS, {"limit": _limit(limit)})
        return tuple(
            TopicKeywordsRow(
                id=as_text(row["id"]),
                label=as_text(row["label"]),
                keywords=_as_words(row["keywords"]),
                message_count=as_int(row["message_count"]),
            )
            for row in rows
        )

    def topics_of(self, ids: Sequence[str]) -> dict[str, TopicMembershipRow]:
        """Which topic each of these messages sits in, keyed by message id.

        The read a listing grouped by topic makes beside its page — one
        statement over the page's ids, the shape
        :meth:`~mailarc_core.archive.reader.ArchiveReader.conversations_of`
        established. A message in no topic is absent; an empty ask opens no
        session. A message the clustering filed twice resolves to the smallest
        topic id, so the group it lands in does not depend on the order the
        rows came back.
        """
        asked = list(dict.fromkeys(ids))
        if not asked:
            return {}
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.TOPICS_OF_MESSAGES, {"ids": asked})
        found: dict[str, TopicMembershipRow] = {}
        for row in rows:
            message_id = as_text(row["message_id"])
            candidate = TopicMembershipRow(
                topic_id=as_text(row["topic_id"]),
                label=as_text(row["label"]),
                keywords=_as_words(row["keywords"]),
            )
            if (
                message_id not in found
                or candidate.topic_id < found[message_id].topic_id
            ):
                found[message_id] = candidate
        return found

    def groups_of(self, ids: Sequence[str]) -> dict[str, GroupMembershipRow]:
        """Which recurring group each of these messages went to, by message id.

        :meth:`topics_of` over ``ADDRESSED_GROUP``, with the same absence and
        the same tie rule — although a message has one ``participant_key`` and
        therefore at most one group, so the tie is a guard rather than a case.
        """
        asked = list(dict.fromkeys(ids))
        if not asked:
            return {}
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.GROUPS_OF_MESSAGES, {"ids": asked})
        found: dict[str, GroupMembershipRow] = {}
        for row in rows:
            message_id = as_text(row["message_id"])
            candidate = GroupMembershipRow(
                group_id=as_text(row["group_id"]),
                size=as_int(row["size"]),
                message_count=as_int(row["message_count"]),
            )
            if (
                message_id not in found
                or candidate.group_id < found[message_id].group_id
            ):
                found[message_id] = candidate
        return found

    def suggestion_counts(self) -> dict[str, int]:
        """How many messages each tag is being offered, keyed by tag id.

        A mapping rather than a row type, because the tag itself is not this
        reader's to describe: ``Tag`` belongs to ``mailarc-core`` and a page
        already holds
        :class:`~mailarc_core.archive.model.TagSummary` values from
        :class:`~mailarc_core.archive.tags.TagStore`. What is missing there is
        the badge, and a badge is a number.

        **Every tag is in it, including the ones with nothing to accept.** The
        statement's traversal is optional, so a tag no analysis had anything to
        say about comes back as a zero — which is a state a user should be
        shown, and absence is not.

        Unlimited, alone among the listings here: the population is the tags a
        person made by hand.
        """
        with self._graph_session() as graph:
            rows = rows_of(graph, catalog.SUGGESTION_COUNTS)
        return {as_text(row["id"]): as_int(row["suggestions"]) for row in rows}

    def suggestions_for(
        self, tag_id: str, *, limit: int = REPORT_LIMIT
    ) -> tuple[TagSuggestionRow, ...]:
        """What one tag is being offered, strongest case first.

        The tag arrives as a **bound parameter** and never as part of a
        statement, which is the catalogue's rule and matters more here than
        anywhere else in this file: this is the one listing whose argument
        comes from something a user typed.
        """
        with self._graph_session() as graph:
            rows = rows_of(
                graph,
                catalog.TAG_SUGGESTIONS,
                {"tag": tag_id, "limit": _limit(limit)},
            )
        return tuple(
            TagSuggestionRow(
                message_id=as_text(row["id"]),
                subject=as_text(row["subject"]),
                sent_at=as_datetime(row["sent_at"]),
                score=as_float(row["score"]),
                method=as_text(row["method"]),
            )
            for row in rows
        )


def _co_recipients(session: Session, limit: int) -> tuple[CoRecipientRow, ...]:
    """A1 off the ground truth. Taken as a session so the cross-check can run
    it beside its counterpart without opening a second driver."""
    return tuple(
        CoRecipientRow(
            left_id=as_text(row["left_id"]),
            right_id=as_text(row["right_id"]),
            together=as_int(row["together"]),
        )
        for row in rows_of(session, catalog.CO_RECIPIENTS, {"limit": limit})
    )


def _co_addressed(session: Session, limit: int) -> tuple[CoAddressedRow, ...]:
    """A1 off the materialised edge, same shape and same session rule."""
    return tuple(
        CoAddressedRow(
            left_id=as_text(row["left_id"]),
            right_id=as_text(row["right_id"]),
            together=as_int(row["together"]),
            first_seen=as_datetime(row["first_seen"]),
            last_seen=as_datetime(row["last_seen"]),
        )
        for row in rows_of(session, catalog.TOP_CO_ADDRESSED, {"limit": limit})
    )


def _count(session: Session, statement: Statement) -> int:
    """One of the catalogue's counting statements, as a number.

    An empty result is zero rather than an error: a count over a label no
    rebuild has written yet is a state, and the page that asks wants a nought
    in a cell.
    """
    rows = rows_of(session, statement)
    return as_int(rows[0]["total"]) if rows else 0


def _today() -> date:
    """The day the archiving window ends on, in UTC.

    A function rather than a ``datetime.now(UTC).date()`` spelled into
    :meth:`AnalyticsReader.archived_per_day`, so the boundary has a name and a
    place to say *which* today it means. It is also the seam the window's tests
    pin: a test that read the clock the same way the reader does would agree
    with it by construction, and fail once a year at midnight.
    """
    return datetime.now(UTC).date()


def _as_day(value: object) -> date | None:
    """One ``YYYY-MM-DD`` key as a date, or ``None`` if it is not one.

    ``left(r.archived_at, 10)`` cuts ten characters off whatever the property
    holds and never fails, so an edge stamped by something other than
    :class:`~mailarc_core.archive.writer.MessageArchiver` comes back here as a
    key no calendar has. One day loses its numbers; the chart keeps its shape.
    """
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        logger.warning("Ignoring unparseable archiving day %r", value)
        return None


def _as_words(value: object) -> tuple[str, ...]:
    """A stored list of strings as a tuple, in the order the rebuild wrote it.

    ``Message.importance_reasons`` and ``Topic.keywords`` are the only list
    properties a report reads, and both are ordered on purpose — the reasons as
    the scorer sorted them, the keywords most discriminating first — so this
    keeps the order and never sorts. A property the rebuild has not written is
    ``None`` and comes back as ``()``: a message scored with no reason at all
    and a message never scored both render as no chips, and the listing already
    filters the second one out.
    """
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(one) for one in value if one is not None)


def _span(first: date, last: date) -> tuple[date, ...]:
    """Every day from *first* to *last*, both ends included."""
    return tuple(first + timedelta(days=one) for one in range((last - first).days + 1))


def _limit(value: int) -> int:
    """A row ceiling the statements can actually be bound to.

    Every one of them binds ``$limit`` into a trailing ``LIMIT``, and
    ``LIMIT 0`` is legal Cypher that returns nothing — so a caller's stray zero
    would render as an empty archive rather than as the mistake it is. One row
    is the smallest answer that still says something. The statements became
    query-builder objects and that stayed exactly true:
    ``.limit(param("limit"))`` compiles to the same clause and the caller's
    number still reaches the store, which was measured rather than assumed.

    Clamped at the top as well as the bottom. This module is a public surface —
    ``AnalyticsReader`` and both limits are exported from ``mailarc_analytics``,
    and phase 6's MCP server serves a model from the same constants — so
    ``limit`` is an argument something other than this repository chooses.
    Unbounded, ``co_addressed_agreement(limit=10_000_000)`` runs the self-join
    over every message and pulls ten million rows into two tuples and two
    dicts, in a thread, beside an in-process FalkorDB. A report is a report.
    """
    return min(max(1, value), MAX_ROWS)


def _ceiling(window: int) -> int:
    """How many day-rows :meth:`AnalyticsReader.archived_per_day` reads.

    Wider than the window it is filling, because the statement's ``ORDER BY day
    DESC`` puts every day-row stamped **after** today in front of the ones the
    window wants. ``archived_at`` is a wall clock somebody else set — a restored
    backup, a machine that ran ahead — so those rows exist, and a ceiling equal
    to the window is spent on them before the real days are reached.

    Twice the window, which tolerates as many skewed days as the chart is wide,
    and never above ``MAX_ROWS``: the window has already been clamped there, and
    a year's window with a year of clock skew behind it is not a case worth
    reading five thousand rows for.
    """
    return min(window * 2, MAX_ROWS)
