"""The one statement whose shape a real backend had to decide.

``ArchivedFrom.archived_at`` is a ``datetime`` field on a mapped edge, and
:data:`~mailarc_analytics.queries.catalog.ARCHIVED_PER_DAY` cuts a day key out
of it with ``left(r.archived_at, 10)``. Whether that yields ``2026-03-01`` or
something no calendar has is not a question ``build()`` can answer — a compiled
statement shows the call, not what the store makes of the property — so it is
answered here, by archiving mail on named days with the real writer and reading
the buckets back.

The alternative the spec authorised was projecting the raw stamp and bucketing
in Python under the ``MAX_ROWS`` ceiling. This file is what made that
unnecessary, and it is also what would show it if a FalkorDB upgrade changed
its mind: the fixture below fails loudly rather than quietly answering with one
bucket per *message*.

Two fixtures, because they ask different things. The shared planted corpus is
archived in one moment, so it proves the aggregation folds thirty-three copies
into one row and that the day key is today's *UTC* date; the stamped one
spreads five copies over three named days, so it proves the key is the day and
not the instant.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import corpus
import planted_graph
import pytest

from mailarc_analytics import AnalyticsReader
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.rows import rows_of
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local

CEILING = 50
"""Rows this file ever asks the statement for.

It returns one per day and no fixture here plants more than three, so any
number above three says the same thing — the ceiling is being bound because it
is declared, not because anything is near it.
"""

STAMPS: tuple[datetime, ...] = (
    datetime(2026, 3, 1, 0, 0, tzinfo=UTC),
    datetime(2026, 3, 1, 23, 59, 59, tzinfo=UTC),
    datetime(2026, 3, 3, 12, 0, tzinfo=UTC),
    datetime(2026, 3, 3, 12, 0, 1, tzinfo=UTC),
    datetime(2026, 3, 5, 6, 30, tzinfo=UTC),
)
"""Five copies over three days, and the first two are the assertion.

Midnight and one second to midnight have nothing in common as instants and have
to land in one bucket, which is the whole claim ``left()`` is being asked to
support. The second of March is left empty on purpose: a statement that
returned a row for it would be inventing one, and filling that gap is the
*reader's* job rather than the store's.
"""

PLANTED = corpus.planted_corpus()[: len(STAMPS)]
"""The mails :data:`STAMPS` is spread over, in the order it stamps them."""


def _size(index: int) -> int:
    """What the parser measures off the raw bytes of the *index*-th mail.

    Read from the corpus rather than written down, so a change to a planted
    body moves the expectation with it instead of turning a byte sum into a
    magic number nobody dares touch.
    """
    return parse_message(PLANTED[index].raw).size_bytes


def _archive_stamped(config: GraphConfig, stamps: Sequence[datetime]) -> None:
    """Archive one planted mail per stamp, on the day that stamp names.

    Written with the real :class:`~mailarc_core.archive.writer.MessageArchiver`
    and the real parser, exactly as ``planted_graph.archive`` does, because a
    hand-written ``MERGE`` would plant an edge no import can produce and then
    prove that the statement reads *that*. The one thing added is the stamp:
    ``ArchiveSource.archived_at`` is what the writer falls back to ``now()``
    for, and nothing in this repository sets it — a sync run always archives in
    the present — so a test is the only place a named day can come from.
    """
    archiver = MessageArchiver(ArchiveConfig())
    with client.session(config) as graph:
        for one, stamp in zip(corpus.planted_corpus(), stamps, strict=False):
            source = planted_graph.source_for(one).model_copy(
                update={"archived_at": stamp}
            )
            archiver.archive(graph, parse_message(one.raw), source)


@pytest.fixture
def stamped(config: GraphConfig) -> GraphConfig:
    """A graph holding five archived copies over the three days
    :data:`STAMPS` names, and nothing else."""
    _archive_stamped(config, STAMPS)
    return config


def _buckets(config: GraphConfig) -> list[dict[str, Any]]:
    """The statement's own answer, bound the way every caller binds it."""
    with client.session(config) as graph:
        return rows_of(graph, catalog.ARCHIVED_PER_DAY, {"limit": CEILING})


class TestWhatTheStoreMakesOfTheStamp:
    """The decision: ``left()`` over a datetime-converted column."""

    def test_a_day_key_is_the_calendar_date_and_nothing_else(
        self, stamped: GraphConfig
    ) -> None:
        """Measured on the vendored FalkorDB, and the reason the reader does no
        bucketing of its own.

        runic's mapper stores an ``ArchivedFrom.archived_at`` as the ISO-8601
        string the writer's ``datetime`` renders to, so the first ten
        characters of the property really are ``YYYY-MM-DD``. Newest first,
        because the statement's ceiling has to keep the recent days.
        """
        found = [row["day"] for row in _buckets(stamped)]

        assert found == ["2026-03-05", "2026-03-03", "2026-03-01"]

    def test_two_instants_on_one_day_are_one_bucket(self, stamped: GraphConfig) -> None:
        """Midnight and one second to midnight — the pair the whole key exists
        to fold together. Five copies, three rows."""
        found = {row["day"]: row["messages"] for row in _buckets(stamped)}

        assert found == {"2026-03-01": 2, "2026-03-03": 2, "2026-03-05": 1}

    def test_the_bytes_of_a_day_are_the_sizes_of_the_copies_archived_on_it(
        self, stamped: GraphConfig
    ) -> None:
        """The second series the dashboard draws, and the one place the
        statement adds something up rather than counting it."""
        found = {row["day"]: row["bytes"] for row in _buckets(stamped)}

        assert found["2026-03-01"] == _size(0) + _size(1)
        assert found["2026-03-03"] == _size(2) + _size(3)
        assert found["2026-03-05"] == _size(4)

    def test_a_day_nothing_was_archived_on_is_absent_rather_than_zero(
        self, stamped: GraphConfig
    ) -> None:
        """The second of March sits between two days that have rows. The store
        answers about what happened; the gap is the reader's to fill, which is
        why :meth:`AnalyticsReader.archived_per_day` exists at all."""
        assert "2026-03-02" not in [row["day"] for row in _buckets(stamped)]

    def test_the_planted_corpus_folds_into_the_one_day_it_was_archived_on(
        self, archived: GraphConfig
    ) -> None:
        """Thirty-three copies, one moment, one row — the aggregation at the
        size the rest of this component's fixtures use.

        Nothing sets ``ArchiveSource.archived_at`` in the shared fixture, so
        the writer stamps ``datetime.now(UTC)`` and the day key is today's
        **UTC** date. That is the property the reader's window rests on, and
        asserting it here is what pins "UTC" to a measurement rather than to a
        docstring. Taken either side of the read, so a run crossing midnight
        reports the day it actually archived on.
        """
        before = datetime.now(UTC).date().isoformat()
        found = _buckets(archived)
        after = datetime.now(UTC).date().isoformat()

        assert len(found) == 1
        assert found[0]["day"] in {before, after}
        assert found[0]["messages"] == len(corpus.planted_corpus())


class TestTheReaderOverARealGraph:
    """The façade's window, over rows a store really produced."""

    def _reader(self, config: GraphConfig) -> AnalyticsReader:
        """Wired the way ``app/composition.py`` wires it."""
        return AnalyticsReader(partial(client.session, config))

    def test_the_window_fills_the_quiet_days_between_the_busy_ones(
        self, config: GraphConfig
    ) -> None:
        """Three copies on two days inside a week, read back as seven rows.

        The stamps are relative to now rather than named, because the window
        ends on today and a fixed date would leave it empty. They stay within
        two days of it so a run crossing UTC midnight moves them one day older
        and still leaves every one of them inside the week.
        """
        now = datetime.now(UTC)
        _archive_stamped(
            config, (now, now - timedelta(days=2), now - timedelta(days=2))
        )

        found = self._reader(config).archived_per_day(days=7)

        assert len(found) == 7
        assert [one.day for one in found] == sorted(one.day for one in found)
        assert sum(one.messages for one in found) == 3
        assert sum(1 for one in found if one.messages == 0) == 5

    def test_archiving_older_than_the_window_is_left_out_of_it(
        self, stamped: GraphConfig
    ) -> None:
        """March 2026 is behind us, so a week ending today holds none of it.

        The statement still returns those three rows — the ceiling is a number
        of rows, not a span of days — and the reader answers with a week of
        zeros rather than with somebody else's spring.
        """
        found = self._reader(stamped).archived_per_day(days=7)

        assert len(found) == 7
        assert sum(one.messages for one in found) == 0
        assert sum(one.bytes for one in found) == 0

    def test_days_stamped_in_the_future_do_not_crowd_the_window_out(
        self, config: GraphConfig
    ) -> None:
        """``archived_at`` is a wall clock somebody else set.

        The statement orders by day descending and stops at its ceiling, so a
        restored backup — or a machine whose clock ran ahead — puts rows in
        front of every day the window actually wants. With the ceiling bound to
        the window itself, three future days ate a three-day window whole and
        the reader gap-filled the real ones as zeros: a chart saying the
        archive took nothing in on days it was taking mail in.

        The two real stamps are today and yesterday, so a run crossing UTC
        midnight moves both one day older and leaves both inside the window.
        """
        now = datetime.now(UTC)
        _archive_stamped(
            config,
            (
                now + timedelta(days=1),
                now + timedelta(days=2),
                now + timedelta(days=3),
                now,
                now - timedelta(days=1),
            ),
        )

        found = self._reader(config).archived_per_day(days=3)

        assert len(found) == 3
        assert sum(one.messages for one in found) == 2

    def test_the_window_ends_on_today_in_utc(self, config: GraphConfig) -> None:
        """The boundary the reader documents, checked against the clock the
        writer stamps with — both read in UTC, so an archive imported this
        morning lands on the last column of the chart and not off the end."""
        before = datetime.now(UTC).date().isoformat()
        found = self._reader(config).archived_per_day(days=3)
        after = datetime.now(UTC).date().isoformat()

        assert found[-1].day in {before, after}
