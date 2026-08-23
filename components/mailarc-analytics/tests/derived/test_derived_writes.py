"""The one shape every derived write takes, checked without a graph.

``merge_rows`` decides three things and nothing else: how many rows go in one
round trip, that an empty batch is not sent at all, and that the rows arrive in
the order the analysis produced them. Each of those is a claim about the loop
rather than about Cypher, so a recording stand-in answers them exactly and a
server would only make them slower to ask.

That the statements really are upserts is a different claim and belongs to
``test_derived_writer_local.py``, where running one twice is measured against
the node counts a real backend reports.
"""

from collections.abc import Mapping
from typing import Any, cast

from runic.ogm import Session

from mailarc_analytics.derived.writes import WRITE_BATCH, merge_rows


class RecordingSession:
    """A ``runic.ogm.Session`` stand-in that runs nothing and remembers all.

    Only ``execute`` is modelled, because that is the only member
    :func:`~mailarc_analytics.derived.writes.merge_rows` reaches for — the same
    approach ``mailarc-core``'s writer test takes with its own fake.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> None:
        self.calls.append((statement, dict(params or {})))

    @property
    def batches(self) -> list[list[Mapping[str, Any]]]:
        """The rows of each round trip, in the order they were sent."""
        return [call[1]["rows"] for call in self.calls]


def _rows(count: int) -> list[Mapping[str, Any]]:
    return [{"id": f"m{index:05d}"} for index in range(count)]


def _session() -> tuple[RecordingSession, Session]:
    """The fake and the same object typed as what the function expects."""
    fake = RecordingSession()
    return fake, cast(Session, fake)


def test_a_short_run_of_rows_is_one_round_trip() -> None:
    fake, session = _session()

    written = merge_rows(session, "STATEMENT", _rows(3))

    assert written == 3
    assert fake.batches == [_rows(3)]


def test_the_statement_and_the_rows_arrive_as_the_caller_built_them() -> None:
    """Bound as ``$rows`` and never interpolated — the catalogue's whole rule.

    An address or a subject that reached the graph as text rather than as a
    parameter could change what the statement *does*, which is the one thing a
    query catalogue exists to make impossible.
    """
    fake, session = _session()

    merge_rows(session, "UNWIND $rows AS row MERGE (g:Group {id: row.id})", _rows(2))

    statement, params = fake.calls[0]
    assert statement == "UNWIND $rows AS row MERGE (g:Group {id: row.id})"
    assert set(params) == {"rows"}


def test_more_rows_than_a_batch_are_cut_into_batches() -> None:
    """So one rebuild over a large archive never holds a parameter payload
    bigger than one batch."""
    fake, session = _session()

    written = merge_rows(session, "STATEMENT", _rows(WRITE_BATCH + 5))

    assert written == WRITE_BATCH + 5
    assert [len(batch) for batch in fake.batches] == [WRITE_BATCH, 5]


def test_a_batch_boundary_that_lands_exactly_sends_no_empty_tail() -> None:
    """``UNWIND []`` is a legal no-op, but a round trip to say nothing is
    still a round trip."""
    fake, session = _session()

    merge_rows(session, "STATEMENT", _rows(WRITE_BATCH))

    assert [len(batch) for batch in fake.batches] == [WRITE_BATCH]


def test_nothing_to_write_is_nothing_sent() -> None:
    """A rebuild of an empty archive must not talk to the graph at all."""
    fake, session = _session()

    assert merge_rows(session, "STATEMENT", []) == 0
    assert fake.calls == []


def test_the_rows_are_consumed_as_they_arrive() -> None:
    """Taken as an iterable, so an analysis can hand over a generator and the
    rebuild never materialises everything it is about to write."""
    fake, session = _session()
    produced: list[str] = []

    def rows() -> Any:
        for row in _rows(3):
            produced.append(row["id"])
            yield row

    merge_rows(session, "STATEMENT", rows())

    assert produced == ["m00000", "m00001", "m00002"]
    assert fake.batches == [_rows(3)]
