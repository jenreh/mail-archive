"""Reading a result set back, without a driver in the room.

Every claim here is about what happens to a value between the wire and a value
object, and none of them needs a server to answer: which of the two ways to run
a statement each kind of entry takes, whether the header really is zipped onto
the rows of the one that comes back raw, whether a result that came back empty
is a list rather than ``None``, and whether a timestamp the writer stored
without a zone can still be compared with one that has one.

That last one is the only assertion in this file that was bought rather than
guessed. A naive value read as naive took a whole rebuild down with "can't
compare offset-naive and offset-aware datetimes" the first time an analysis
took a ``min()`` over the archive's dates, so it is pinned here as well as in
``test_derived_reader_local.py``.
"""

import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.rows import (
    _IN_USE,
    _in_use,
    as_datetime,
    as_float,
    as_int,
    as_text,
    rows_of,
)


class Answering:
    """A ``runic.ogm.Session`` stand-in that hands back one prepared result.

    Both ways of running a statement are modelled, because :func:`rows_of` is
    the dispatch between them and a stand-in that only knew one would let the
    wrong path pass unnoticed. ``execute`` answers the way a driver does — a
    header and a list of lists, the two members every
    :class:`~runic.ogm.driver.GraphResult` promises — and ``all_rows`` answers
    the way a session does, with the zip already done.
    """

    def __init__(self, columns: list[str], rows: list[list[Any]] | None) -> None:
        self.columns = columns
        self.rows = rows
        self.calls: list[tuple[Any, dict[str, Any] | None]] = []
        self.bound: list[Any] = []

    def execute(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> Answering:
        self.calls.append((statement, dict(params or {})))
        return self

    def all_rows(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.bound.append(statement)
        self.calls.append((statement, None if params is None else dict(params)))
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows or []]


class Built:
    """Anything that is not a string — which is what the dispatch turns on.

    A real :class:`~runic.ogm.QueryBuilder` would work here too and would say
    less: what :func:`rows_of` asks is whether it was handed raw Cypher, and
    the answer for every catalogue entry but one is "no".
    """


def _statement() -> Statement:
    """One stand-in statement, typed as what ``rows_of`` takes.

    A cast at the seam, the same way :func:`_as` types the session: the
    dispatch turns on ``isinstance(statement, str)`` and nothing else, so a
    stand-in says what these tests are about more plainly than a real builder
    would.
    """
    return cast(Statement, Built())


NAIVE = datetime(2026, 1, 12, 9, 0, tzinfo=UTC).replace(tzinfo=None)
"""A timestamp with no zone at all — what a graph written by something other
than runic's mapper can hold. Built by stripping a zone rather than by leaving
one out, so the linter's guard against accidentally naive datetimes stays on
for the rest of the file."""


def _session(columns: list[str], rows: list[list[Any]] | None) -> Answering:
    return Answering(columns, rows)


def _as(fake: Answering) -> Session:
    """The fake, typed as what the function under test expects."""
    return cast(Session, fake)


def _lock_on(statement: Statement) -> threading.RLock:
    """The lock :func:`rows_of` would take for *statement*.

    A cast at the seam, the same one :func:`_as` and :func:`_statement` make:
    :func:`_in_use` is only ever reached with the builder half of the union,
    because :func:`rows_of` answers the ``str`` half before it gets there.
    """
    return _in_use(cast(Any, statement))


def _held_elsewhere(statement: Statement) -> bool:
    """Is this statement's lock held by somebody other than this thread?

    Asked from a thread of its own, because the lock is re-entrant: the thread
    that holds it would simply be handed it again and learn nothing.
    """
    answer: list[bool] = []

    def ask() -> None:
        lock = _lock_on(statement)
        got = lock.acquire(blocking=False)
        if got:
            lock.release()
        answer.append(not got)

    asking = threading.Thread(target=ask)
    asking.start()
    asking.join()
    return answer[0]


class Watching(Answering):
    """An :class:`Answering` that looks at a lock while it is answering.

    A subclass rather than a patched attribute, because what is being asserted
    happens *during* ``all_rows`` — the window :func:`rows_of` holds the lock
    across is exactly the call, so the observation has to be made from inside
    it.
    """

    def __init__(
        self, columns: list[str], rows: list[list[Any]] | None, watched: Statement
    ) -> None:
        super().__init__(columns, rows)
        self.watched = watched
        self.seen: list[bool] = []

    def all_rows(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.seen.append(_held_elsewhere(self.watched))
        return super().all_rows(statement, params)


class Failing(Answering):
    """An :class:`Answering` that refuses, the way a store having a bad day
    does. What matters is what :func:`rows_of` leaves behind afterwards."""

    def all_rows(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        raise RuntimeError("the store said no")


class TestRunningABuilderStatement:
    """The path every catalogue entry but one takes: the session binds it."""

    def test_it_goes_to_all_rows_and_never_to_the_driver(self) -> None:
        """``session.execute`` cannot run a statement object at all — the
        driver concatenates it onto a string and raises — and
        ``session.execute(*statement.build())`` reaches the store *without* the
        declared parameters. Either way the store answers with an error rather
        than with rows, so the dispatch is what keeps the catalogue runnable.
        """
        fake = _session(["total"], [[3]])
        statement = _statement()

        found = rows_of(_as(fake), statement, {"limit": 10})

        assert fake.bound == [statement]
        assert found == [{"total": 3}]

    def test_the_parameters_reach_the_session_untouched(self) -> None:
        """Declared on the statement and bound by the session, so a binding
        that left one out raises instead of passing a null."""
        fake = _session(["total"], [])
        statement = _statement()

        rows_of(_as(fake), statement, {"after": "", "limit": 10})

        assert fake.calls == [(statement, {"after": "", "limit": 10})]

    def test_a_statement_that_binds_nothing_is_asked_with_nothing(self) -> None:
        """``CLEAR_EMBEDDINGS`` and the four counts take no parameters at all,
        and the session is left to bind whatever the statement fixed itself."""
        fake = _session(["cleared"], [[0]])
        statement = _statement()

        rows_of(_as(fake), statement)

        assert fake.calls == [(statement, None)]

    def test_a_write_with_nothing_to_return_is_an_empty_list(self) -> None:
        """``all_rows`` runs a ``MERGE`` as happily as a ``MATCH`` and answers
        with the ``RETURN`` clause's rows, of which a bare upsert has none."""
        assert rows_of(_as(_session([], [])), _statement()) == []


class TestOneThreadAtATimePerStatement:
    """The window a builder statement opens that a Cypher string never did.

    ``Session.all_rows`` runs ``with stmt._bound_to(self)``, which assigns
    ``stmt._session`` and restores what it found. The catalogue's statements
    are module-level constants, so two threads running the *same* one can have
    the first to leave restore ``None`` under the second — measured against the
    vendored FalkorDB as ``AttributeError: 'NoneType' object has no attribute
    'mapper'``, once in 3200 executions across eight threads, and never once in
    the same load through :func:`rows_of`.

    Asserted here without a race, because a test that reproduces a one-in-3200
    interleaving is a test that fails for other people on other days. What is
    checkable is the mechanism: the lock is *held* while the session is
    working, it is the statement's own, and the raw entry does not take one.
    """

    def test_the_statement_is_locked_while_the_session_runs_it(self) -> None:
        """Held for the whole call, which is the round trip that races."""
        statement = _statement()
        fake = Watching(["id"], [["m1"]], statement)

        assert not _held_elsewhere(statement)
        rows_of(_as(fake), statement, {"limit": 5})

        assert fake.seen == [True]
        assert not _held_elsewhere(statement)

    def test_the_lock_is_released_when_the_session_raises(self) -> None:
        """A failed read must not lock the statement out for the process."""
        statement = _statement()
        fake = Failing(["id"], [["m1"]])

        with pytest.raises(RuntimeError):
            rows_of(_as(fake), statement, {"limit": 5})

        assert not _held_elsewhere(statement)

    def test_two_statements_never_wait_on_each_other(self) -> None:
        """Per statement and not per catalogue.

        A rebuild paging through one statement for minutes must not stall a
        page reading a different one — which a single lock would have done.
        """
        one, other = _statement(), _statement()
        fake = Watching(["id"], [["m1"]], other)

        rows_of(_as(fake), one, {"limit": 5})

        assert fake.seen == [False]
        assert _lock_on(one) is not _lock_on(other)

    def test_the_same_statement_is_always_the_same_lock(self) -> None:
        """Identity is the key — the thing being guarded is the object."""
        statement = _statement()

        assert _lock_on(statement) is _lock_on(statement)

    def test_the_raw_entry_is_not_locked_at_all(self) -> None:
        """A ``str`` is immutable and ``execute`` binds nothing onto it.

        Counted rather than emptied: the map is the process's, and the other
        tests in this class have put their own stand-ins in it. What is being
        asserted is that running the raw entry adds nothing — a ``str`` cannot
        be weakly referenced, so a lock keyed on one would raise rather than
        pass silently.
        """
        fake = _session(["label"], [["Message"]])
        before = len(_IN_USE)

        rows_of(_as(fake), catalog.VECTOR_INDEX_OPTIONS)

        assert len(_IN_USE) == before


class TestZippingTheHeaderOntoTheRows:
    """The other path: the one entry that is still raw Cypher.

    :data:`~mailarc_analytics.queries.catalog.VECTOR_INDEX_OPTIONS` reads the
    live vector index's dimension, which ``IndexOperations.describe()`` cannot
    report, so it goes through ``session.execute`` and gets none of runic's
    mapping — this is that mapping.
    """

    def test_each_row_is_keyed_by_the_column_the_statement_named(self) -> None:
        fake = _session(["left_id", "right_id", "together"], [["a", "b", 4]])

        found = rows_of(_as(fake), "STATEMENT")

        assert found == [{"left_id": "a", "right_id": "b", "together": 4}]

    def test_a_result_with_no_rows_is_an_empty_list(self) -> None:
        """``None`` is what a driver answers when nothing matched, and every
        caller above wants to iterate — an archive nothing has been derived
        from is a state, not a failure."""
        assert rows_of(_as(_session(["total"], None)), "STATEMENT") == []

    def test_the_parameters_reach_the_session_as_the_caller_bound_them(self) -> None:
        """The catalogue's whole rule: caller input arrives as a bound
        ``$parameter``, never as text inside the statement."""
        fake = _session(["total"], [])

        rows_of(_as(fake), "STATEMENT", {"limit": 10})

        assert fake.calls == [("STATEMENT", {"limit": 10})]

    def test_no_parameters_is_an_empty_mapping_and_not_none(self) -> None:
        fake = _session(["total"], [])

        rows_of(_as(fake), "STATEMENT")

        assert fake.calls == [("STATEMENT", {})]

    def test_a_row_that_does_not_match_the_header_is_an_error(self) -> None:
        """``strict=True`` on the zip, so a statement whose columns and rows
        disagree fails here rather than silently dropping a column three layers
        up."""
        fake = _session(["left_id", "right_id"], [["a", "b", 4]])

        with pytest.raises(ValueError, match="longer"):
            rows_of(_as(fake), "STATEMENT")


class TestReadingATimestampBack:
    """Aware or nothing — the rule the rest of the package leans on."""

    def test_an_iso_string_with_an_offset_comes_back_as_that_instant(self) -> None:
        found = as_datetime("2026-01-12T09:00:00+00:00")

        assert found == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

    def test_an_offset_that_is_not_utc_is_kept(self) -> None:
        found = as_datetime("2026-01-12T10:00:00+01:00")

        assert found == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

    def test_a_string_without_a_zone_is_read_as_utc(self) -> None:
        """The expensive case, and the reason this function exists at all: one
        naive value meeting an aware one raises in every ``min``, ``max`` and
        ``sorted`` over the archive's dates, which takes out a whole listing
        rather than one row."""
        found = as_datetime("2026-01-12T09:00:00")

        assert found == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

    def test_a_datetime_that_arrives_as_an_object_is_made_aware_too(self) -> None:
        """A driver that decodes timestamps itself must not slip a naive value
        past the boundary the string path guards."""
        found = as_datetime(NAIVE)

        assert found == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

    def test_an_aware_datetime_object_is_left_exactly_as_it_is(self) -> None:
        berlin = timezone(timedelta(hours=1))
        value = datetime(2026, 1, 12, 10, 0, tzinfo=berlin)

        assert as_datetime(value) is value

    def test_every_value_it_returns_can_be_compared_with_every_other(self) -> None:
        """The property all four paths exist for, asserted as one statement."""
        found = [
            as_datetime("2026-01-12T09:00:00+00:00"),
            as_datetime("2026-01-12T09:00:00"),
            as_datetime(NAIVE.replace(month=3)),
        ]

        assert min(one for one in found if one is not None)

    @pytest.mark.parametrize("value", [None, "", 0])
    def test_a_missing_timestamp_is_none(self, value: Any) -> None:
        assert as_datetime(value) is None

    def test_a_value_that_does_not_parse_costs_one_row_its_date(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A report that died over one malformed property would be worse than
        one that shows a row without a span — but it says so."""
        with caplog.at_level(logging.WARNING):
            found = as_datetime("letzte Woche")

        assert found is None
        assert "letzte Woche" in caplog.text


class TestReadingAScalarBack:
    """What a column holds when the property was never written."""

    @pytest.mark.parametrize(("value", "expected"), [(None, 0), (4, 4), ("7", 7)])
    def test_a_count_is_a_number_and_a_missing_property_is_zero(
        self, value: Any, expected: int
    ) -> None:
        assert as_int(value) == expected

    def test_a_count_column_holding_text_is_an_error(self) -> None:
        """Not a state an archive can be in — a broken statement, and one that
        should say so where it broke."""
        with pytest.raises(ValueError, match="invalid literal"):
            as_int("viele")

    @pytest.mark.parametrize(
        ("value", "expected"), [(None, 0.0), (0.641072, 0.641072), (1, 1.0)]
    )
    def test_a_score_is_a_float_and_a_missing_property_scores_zero(
        self, value: Any, expected: float
    ) -> None:
        assert as_float(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"), [(None, ""), ("", ""), ("ref", "ref"), (7, "7")]
    )
    def test_a_text_column_is_never_none(self, value: Any, expected: str) -> None:
        """So nothing above has to decide between "no label" and "an empty
        label"; for everything this package reports they are the same thing."""
        assert as_text(value) == expected
