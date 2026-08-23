"""Asking the archive what the three analyses found — the read façade.

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

from runic.ogm import Session

from mailarc_analytics.derived.model import TemplateDirection
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.model import (
    ArchiveTotals,
    CoAddressedAgreement,
    CoAddressedRow,
    CoRecipientRow,
    GroupRow,
    TemplateRow,
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

    Five listings, six counts and one verdict.

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

    def templates(
        self, direction: TemplateDirection, *, limit: int = REPORT_LIMIT
    ) -> tuple[TemplateRow, ...]:
        """What is worth automating in one direction, best candidate first.

        One direction per call and never both, because §6.3 asks for them
        apart: only what you write yourself is automatable, and the scores are
        calibrated within a direction and mean nothing across one. The
        direction is bound as its ``str`` value rather than as the enum, for
        the reason :func:`~mailarc_analytics.queries.catalog.as_graph_datetime`
        exists — a raw statement's parameters reach the driver unconverted.
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


def _count(session: Session, statement: str) -> int:
    """One of the catalogue's counting statements, as a number.

    An empty result is zero rather than an error: a count over a label no
    rebuild has written yet is a state, and the page that asks wants a nought
    in a cell.
    """
    rows = rows_of(session, statement)
    return as_int(rows[0]["total"]) if rows else 0


def _limit(value: int) -> int:
    """A row ceiling the statements can actually be bound to.

    Every one of them ends in ``LIMIT $limit``, and ``LIMIT 0`` is legal Cypher
    that returns nothing — so a caller's stray zero would render as an empty
    archive rather than as the mistake it is. One row is the smallest answer
    that still says something.

    Clamped at the top as well as the bottom. This module is a public surface —
    ``AnalyticsReader`` and both limits are exported from ``mailarc_analytics``,
    and phase 6's MCP server serves a model from the same constants — so
    ``limit`` is an argument something other than this repository chooses.
    Unbounded, ``co_addressed_agreement(limit=10_000_000)`` runs the self-join
    over every message and pulls ten million rows into two tuples and two
    dicts, in a thread, beside an in-process FalkorDB. A report is a report.
    """
    return min(max(1, value), MAX_ROWS)
