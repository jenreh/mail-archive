"""Reading a raw result set back, without a driver in the room.

Every claim here is about what happens to a value between the wire and a value
object, and none of them needs a server to answer: whether the header really is
zipped onto the rows, whether a result that came back empty is a list rather
than ``None``, and whether a timestamp the writer stored without a zone can
still be compared with one that has one.

That last one is the only assertion in this file that was bought rather than
guessed. A naive value read as naive took a whole rebuild down with "can't
compare offset-naive and offset-aware datetimes" the first time an analysis
took a ``min()`` over the archive's dates, so it is pinned here as well as in
``test_derived_reader_local.py``.
"""

import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from runic.ogm import Session

from mailarc_analytics.queries.rows import (
    as_datetime,
    as_float,
    as_int,
    as_text,
    rows_of,
)


class Answering:
    """A ``runic.ogm.Session`` stand-in that hands back one prepared result.

    Only ``execute`` is modelled, the way ``test_derived_writes.py`` models
    only what its function reaches for, and the result carries the two members
    every driver's :class:`~runic.ogm.driver.GraphResult` promises.
    """

    def __init__(self, columns: list[str], rows: list[list[Any]] | None) -> None:
        self.columns = columns
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self, statement: str, params: dict[str, Any] | None = None
    ) -> Answering:
        self.calls.append((statement, dict(params or {})))
        return self


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


class TestZippingTheHeaderOntoTheRows:
    """A raw statement gets none of runic's mapping, so this is the mapping."""

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
