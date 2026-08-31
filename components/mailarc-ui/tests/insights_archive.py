"""The canned archive both insights test modules read through.

A shared module rather than a ``conftest``: what is below belongs to the three
insights test files and to no others, and a ``conftest`` in this directory
would put a ``graph`` and a ``published`` in scope for the accounts, import and
review tests that have their own idea of what an archive is.
``mailarc-analytics`` does the same with its ``planted_graph``.

``FakeGraph`` is the part the search tests borrow: a fake session that answers
by statement.

The reader under it is the real :class:`AnalyticsReader`. Only the session is
a fake, and it answers by the catalogue constant it was asked for — so a test
that plants rows for ``TOP_CO_ADDRESSED`` also proves the reader ran that
statement and not another.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from appkit_commons.registry import service_registry

from mailarc_analytics import AnalyticsReader, TemplateDirection, TopicSignal
from mailarc_analytics.queries import catalog
from mailarc_core.archive.reader import GraphSessionFactory

MARCH = datetime(2026, 3, 12, 9, 0, tzinfo=UTC)
AUGUST = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)

COUNTS = (
    catalog.COUNT_MESSAGES,
    catalog.COUNT_UNIDENTIFIED,
    catalog.COUNT_GROUPS,
    catalog.COUNT_TOPICS,
    catalog.COUNT_TEMPLATES,
    catalog.COUNT_CO_ADDRESSED,
)
"""The six statements :meth:`AnalyticsReader.totals` runs, and the whole of
what a page over an archive nobody rebuilt is allowed to ask for."""


class FakeResult:
    """What ``Session.execute`` hands back: a header and a list of lists.

    Still how an answer is written down although
    :meth:`FakeGraph.all_rows` hands back dicts: the header is what the store
    really sends, the zip is what the session really does, and a scripted
    answer reads better column by column.
    """

    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows


class FakeGraph:
    """A `runic.ogm.Session` stand-in that answers one statement at a time.

    Keyed by the catalogue constant itself, so a test that plants rows for
    ``TOP_CO_ADDRESSED`` also proves the reader ran that statement and not
    another. Doubles as its own session factory, the way the review tests'
    fake does.
    """

    def __init__(self) -> None:
        self.answers: dict[Any, FakeResult] = {}
        self.templates: dict[str, FakeResult] = {}
        self.asked: list[Any] = []
        self.params: list[dict[str, Any]] = []
        self.failing: set[Any] = set()
        self.failing_from: dict[Any, int] = {}
        """Statement -> the execution number it starts failing at, counting
        from one.

        ``TOP_CO_ADDRESSED`` runs twice in one refresh — once inside
        ``co_addressed_agreement`` for the verdict and once for the listing
        under it — and :attr:`failing` cannot tell those apart, so a listing
        that failed on its own was unreachable. In production they are two
        round trips and a failure between them is exactly the case the
        listing's own error string exists for.
        """
        self.executions: dict[Any, int] = {}

    def count(self, statement: Any, total: int) -> None:
        self.answers[statement] = FakeResult(["total"], [[total]])

    def rows(self, statement: Any, columns: list[str], rows: list[list[Any]]) -> None:
        self.answers[statement] = FakeResult(columns, rows)

    def template_rows(
        self, direction: TemplateDirection, rows: list[list[Any]]
    ) -> None:
        self.templates[direction.value] = FakeResult(
            [
                "id",
                "occurrences",
                "automation_score",
                "sample_text",
                "first_seen",
                "last_seen",
            ],
            rows,
        )

    def __enter__(self) -> FakeGraph:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def all_rows(
        self, statement: Any, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """How a catalogue statement is run: bound by the session, keyed by
        column.

        Every statement the insights pages reach for is a query-builder object
        that only the session can bind — the driver cannot be handed one — so
        this is the member :func:`~mailarc_analytics.queries.rows.rows_of`
        calls for all of them. :meth:`execute` stays below for the one entry
        that is still raw Cypher, and both answer from the same script.
        """
        answer = self._answer(statement, params or {})
        return [dict(zip(answer.columns, row, strict=True)) for row in answer.rows]

    def execute(self, statement: str, params: dict[str, Any]) -> FakeResult:
        """How the one raw statement is run: a header and a list of lists."""
        return self._answer(statement, params)

    def _answer(self, statement: Any, params: dict[str, Any]) -> FakeResult:
        """The scripted answer for *statement*, recorded, counted and possibly
        made to fail."""
        self.asked.append(statement)
        self.params.append(params)
        self.executions[statement] = self.executions.get(statement, 0) + 1
        if statement in self.failing:
            raise ConnectionError("graph is down")
        if self.executions[statement] >= self.failing_from.get(statement, 1 << 30):
            raise ConnectionError("graph went away between the two reads")
        if statement == catalog.TOP_TEMPLATES:
            return self.templates.get(
                str(params.get("direction", "")), FakeResult([], [])
            )
        return self.answers.get(statement, FakeResult([], []))


def pairs(graph: FakeGraph, truth: list[list[Any]], edge: list[list[Any]]) -> None:
    """Plant both readings of A1 — the self-join's, and the edge's."""
    graph.rows(catalog.CO_RECIPIENTS, ["left_id", "right_id", "together"], truth)
    graph.rows(
        catalog.TOP_CO_ADDRESSED,
        ["left_id", "right_id", "together", "first_seen", "last_seen"],
        edge,
    )


@pytest.fixture
def graph() -> FakeGraph:
    """An archive with a rebuilt derived layer that agrees with itself."""
    built = FakeGraph()
    built.count(catalog.COUNT_MESSAGES, 12)
    built.count(catalog.COUNT_UNIDENTIFIED, 1)
    built.count(catalog.COUNT_GROUPS, 2)
    built.count(catalog.COUNT_TOPICS, 2)
    built.count(catalog.COUNT_TEMPLATES, 2)
    built.count(catalog.COUNT_CO_ADDRESSED, 3)
    agreeing = [
        ["anna@example.com", "bob@example.com", 5],
        ["anna@example.com", "carl@example.com", 3],
        ["bob@example.com", "dora@example.com", 2],
    ]
    pairs(
        built,
        agreeing,
        [[*row, MARCH.isoformat(), AUGUST.isoformat()] for row in agreeing],
    )
    built.rows(
        catalog.RECURRING_GROUPS,
        ["id", "size", "message_count", "first_seen", "last_seen"],
        [
            ["a" * 64, 4, 9, MARCH.isoformat(), AUGUST.isoformat()],
            ["b" * 64, 3, 4, None, None],
        ],
    )
    built.rows(
        catalog.TOPIC_BREAKDOWN,
        ["id", "label", "method", "messages"],
        [
            ["topic:" + "c" * 32, "rechnung swiftscan", TopicSignal.REF.value, 6],
            ["topic:" + "d" * 32, "", "participants", 3],
        ],
    )
    built.template_rows(
        TemplateDirection.SENT,
        [
            [
                "template:1a2b3c4d5e6f7a8b:sent",
                7,
                0.72,
                "Sehr geehrte Damen und Herren,\n\nanbei die Rechnung.",
                MARCH.isoformat(),
                AUGUST.isoformat(),
            ],
        ],
    )
    built.template_rows(
        TemplateDirection.RECEIVED,
        [["template:99ff:received", 4, 0.31, "Ihre Bestellung", None, None]],
    )
    return built


@pytest.fixture
def fresh(graph: FakeGraph) -> FakeGraph:
    """The same archive before anybody ran a rebuild: mail, nothing derived."""
    for statement in (
        catalog.COUNT_GROUPS,
        catalog.COUNT_TOPICS,
        catalog.COUNT_TEMPLATES,
        catalog.COUNT_CO_ADDRESSED,
    ):
        graph.count(statement, 0)
    return graph


@pytest.fixture
def published(graph: FakeGraph) -> Iterator[AnalyticsReader]:
    """The reader, left where the composition root would leave it."""
    registry = service_registry()
    saved = registry.snapshot()
    reader = AnalyticsReader(cast(GraphSessionFactory, lambda: graph))
    registry.register_as(AnalyticsReader, reader)
    yield reader
    registry.restore(saved)
