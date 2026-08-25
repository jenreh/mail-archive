"""The one shape every derived write takes, checked without a graph.

``merge_rows`` decides four things and nothing else: how many rows go in one
round trip, that an empty batch is not sent at all, that the rows arrive in the
order the analysis produced them, and that every batch is encoded under the
model it describes before it is bound. Each of those is a claim about the loop
rather than about Cypher, so a recording stand-in answers them exactly and a
server would only make them slower to ask.

That the statements really are upserts is a different claim and belongs to
``test_derived_writer_local.py``, where running one twice is measured against
the node counts a real backend reports.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from runic.ogm import QueryBuilder, Session

from mailarc_analytics.derived.model import Group
from mailarc_analytics.derived.writes import WRITE_BATCH, merge_rows
from mailarc_analytics.queries import catalog

STATEMENT = catalog.MERGE_GROUPS
"""A real catalogue statement, because a string is not one any more.

``merge_rows`` runs whatever it is handed through ``session.all_rows``, and a
test that passed a string would be exercising the other half of ``rows_of`` —
the one raw statement's path — rather than the one every analysis takes.
"""


class RecordingSession:
    """A ``runic.ogm.Session`` stand-in that runs nothing and remembers all.

    Only ``all_rows`` is modelled, because that is the only member
    :func:`~mailarc_analytics.derived.writes.merge_rows` reaches for — the same
    approach ``mailarc-core``'s writer test takes with its own fake. It is
    ``all_rows`` and not ``execute`` because a catalogue statement declares its
    ``$rows`` parameter: only the session can bind one, and the driver would
    refuse the builder object outright.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[QueryBuilder[Any] | str, Mapping[str, Any]]] = []

    def all_rows(
        self,
        statement: QueryBuilder[Any] | str,
        params: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
        return []

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

    written = merge_rows(session, STATEMENT, _rows(3), model=Group)

    assert written == 3
    assert fake.batches == [_rows(3)]


def test_the_statement_and_the_rows_arrive_as_the_caller_built_them() -> None:
    """Bound as ``$rows`` and never interpolated — the catalogue's whole rule.

    An address or a subject that reached the graph as text rather than as a
    parameter could change what the statement *does*, which is the one thing a
    query catalogue exists to make impossible. The statement arrives as the
    catalogue's own object, unmodified: a builder mutates in place, so a
    ``merge_rows`` that narrowed or extended one would be editing the catalogue
    entry for the rest of the process.
    """
    fake, session = _session()

    merge_rows(session, STATEMENT, _rows(2), model=Group)

    statement, params = fake.calls[0]
    assert statement is catalog.MERGE_GROUPS
    assert set(params) == {"rows"}


def test_a_timestamp_is_encoded_under_the_model_before_it_is_bound() -> None:
    """An ``UNWIND`` payload never passes through runic's mapper on its own.

    A bare ``datetime`` in it reaches the driver as an object the store has no
    encoding for and the whole payload is refused with ``Failed to parse query
    parameter 'rows' value``. ``encode_rows`` applies the model's own
    converters, which is what turns it into the ISO-8601 string every other
    date in the graph is stored as.
    """
    fake, session = _session()

    merge_rows(
        session,
        STATEMENT,
        [{"id": "circle", "first_seen": datetime(2026, 3, 4, 9, 15, tzinfo=UTC)}],
        model=Group,
    )

    assert fake.batches == [
        [{"id": "circle", "first_seen": "2026-03-04T09:15:00+00:00"}]
    ]


def test_a_row_that_is_already_encoded_is_left_exactly_as_it_is() -> None:
    """Which is what let the three analyses move over one at a time.

    They used to hand ISO-8601 strings in by calling ``as_graph_datetime``
    themselves; encoding one of those again is a no-op, so a payload built
    either way reaches the store identically.
    """
    fake, session = _session()

    merge_rows(
        session,
        STATEMENT,
        [{"id": "circle", "first_seen": "2026-03-04T09:15:00+00:00"}],
        model=Group,
    )

    assert fake.batches == [
        [{"id": "circle", "first_seen": "2026-03-04T09:15:00+00:00"}]
    ]


def test_a_key_the_model_does_not_declare_is_passed_through() -> None:
    """What an edge row is made of: two endpoint ids no edge model declares.

    ``encode_rows`` converts declared fields and leaves everything else alone,
    which is why one encoding step can serve all seven merges — the three node
    upserts and the four edge ones.
    """
    fake, session = _session()

    merge_rows(
        session, STATEMENT, [{"message_id": "m1", "group_id": "g1"}], model=Group
    )

    assert fake.batches == [[{"message_id": "m1", "group_id": "g1"}]]


def test_more_rows_than_a_batch_are_cut_into_batches() -> None:
    """So one rebuild over a large archive never holds a parameter payload
    bigger than one batch."""
    fake, session = _session()

    written = merge_rows(session, STATEMENT, _rows(WRITE_BATCH + 5), model=Group)

    assert written == WRITE_BATCH + 5
    assert [len(batch) for batch in fake.batches] == [WRITE_BATCH, 5]


def test_a_batch_boundary_that_lands_exactly_sends_no_empty_tail() -> None:
    """``UNWIND []`` is a legal no-op, but a round trip to say nothing is
    still a round trip."""
    fake, session = _session()

    merge_rows(session, STATEMENT, _rows(WRITE_BATCH), model=Group)

    assert [len(batch) for batch in fake.batches] == [WRITE_BATCH]


def test_nothing_to_write_is_nothing_sent() -> None:
    """A rebuild of an empty archive must not talk to the graph at all."""
    fake, session = _session()

    assert merge_rows(session, STATEMENT, [], model=Group) == 0
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

    merge_rows(session, STATEMENT, rows(), model=Group)

    assert produced == ["m00000", "m00001", "m00002"]
    assert fake.batches == [_rows(3)]
