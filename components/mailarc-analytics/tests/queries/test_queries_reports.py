"""The façade's own decisions, with a scripted session instead of a graph.

Three kinds of claim live here and none of them needs a server. What each
method *asks* — which statement, with which parameters bound, in how many
sessions — is a property of this module and a recording stand-in answers it
exactly. What each method *decodes* is a property of the row builders, and a
scripted result set says it in one line where a planted corpus would say it in
thirty. And that a caller can never hand this class Cypher is a property of the
source text, so it is read off the source text.

Whether the statements return what these fakes pretend they return is the
question ``test_queries_catalog_local.py`` already answers, and whether the
numbers are the planted ones is ``test_queries_reports_local.py``'s.
"""

import ast
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict
from runic.ogm import Session

from mailarc_analytics.derived.model import TemplateDirection
from mailarc_analytics.queries import catalog, reports
from mailarc_analytics.queries.catalog import CATALOG
from mailarc_analytics.queries.reports import AGREEMENT_LIMIT, AnalyticsReader

WHEN = "2026-01-12T09:00:00+00:00"
LATER = "2026-02-10T16:00:00+00:00"

_CYPHER = re.compile(r"\b(MATCH|MERGE|CREATE|DELETE|DETACH|UNWIND|RETURN|SET)\b")
"""A clause no value in the façade may contain.

Deliberately clause keywords rather than "looks like a query": the point is not
to recognise Cypher, it is that a statement is a named constant in the
catalogue or it does not exist, and a hand-written one always starts with one
of these.
"""


class Reply(BaseModel):
    """One statement's answer in the shape every driver promises: a header and
    a list of lists, with no entity mapping anywhere near it."""

    model_config = ConfigDict(frozen=True)

    columns: list[str] = []
    rows: list[list[Any]] = []


class Scripted:
    """A ``runic.ogm.Session`` stand-in that answers from a script and
    remembers what it was asked, plus the factory that hands it out.

    It counts how often a session was *opened* as well, because "one session
    per question" is one of the things this module decides: six counts read
    through six drivers would be six connections for one row of numbers, and
    the two halves of the cross-check read through two would be a comparison of
    two different moments.
    """

    def __init__(self, answers: Mapping[str, Reply] | None = None) -> None:
        self._answers = dict(answers or {})
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.opened = 0

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> Reply:
        self.asked.append((statement, dict(params or {})))
        return self._answers.get(statement, Reply())

    @contextmanager
    def open(self) -> Iterator[Session]:
        self.opened += 1
        yield cast(Session, self)

    @property
    def statements(self) -> list[str]:
        return [statement for statement, _ in self.asked]

    def parameters(self, statement: str) -> dict[str, Any]:
        """What the one call to *statement* bound."""
        return next(params for asked, params in self.asked if asked is statement)


def _reader(
    answers: Mapping[str, Reply] | None = None,
) -> tuple[Scripted, AnalyticsReader]:
    fake = Scripted(answers)
    return fake, AnalyticsReader(fake.open)


def _total(value: int) -> Reply:
    return Reply(columns=["total"], rows=[[value]])


class TestWhatTheTotalsAsk:
    """Six statements, one session, one moment."""

    def test_every_count_comes_back_under_its_own_name(self) -> None:
        fake, reader = _reader(
            {
                catalog.COUNT_MESSAGES: _total(33),
                catalog.COUNT_UNIDENTIFIED: _total(1),
                catalog.COUNT_GROUPS: _total(2),
                catalog.COUNT_TOPICS: _total(1),
                catalog.COUNT_TEMPLATES: _total(2),
                catalog.COUNT_CO_ADDRESSED: _total(3),
            }
        )

        found = reader.totals()

        assert found.messages == 33
        assert found.unidentified == 1
        assert (found.groups, found.topics, found.templates) == (2, 1, 2)
        assert found.co_addressed == 3
        assert fake.opened == 1

    def test_a_count_over_a_label_nothing_has_written_is_zero(self) -> None:
        """An archive nobody has run a rebuild over is a state, and the page
        that asks wants a nought in a cell rather than an exception."""
        _fake, reader = _reader()

        assert reader.totals().derived == 0


class TestWhatEachListingAsks:
    """One statement, its parameters bound, nothing interpolated."""

    def test_the_ground_truth_pairs_bind_only_a_limit(self) -> None:
        fake, reader = _reader()

        reader.co_recipients(limit=10)

        assert fake.statements == [catalog.CO_RECIPIENTS]
        assert fake.parameters(catalog.CO_RECIPIENTS) == {"limit": 10}

    def test_the_stored_pairs_bind_only_a_limit(self) -> None:
        fake, reader = _reader()

        reader.top_co_addressed(limit=10)

        assert fake.parameters(catalog.TOP_CO_ADDRESSED) == {"limit": 10}

    def test_the_groups_bind_both_thresholds_and_a_limit(self) -> None:
        fake, reader = _reader()

        reader.recurring_groups(min_size=3, min_messages=2, limit=10)

        assert fake.parameters(catalog.RECURRING_GROUPS) == {
            "min_size": 3,
            "min_messages": 2,
            "limit": 10,
        }

    def test_the_groups_default_to_everything_the_rebuild_kept(self) -> None:
        """The thresholds that decided which groups exist were applied when
        they were written; repeating them here would be a second copy, and the
        copy is the one that drifts."""
        fake, reader = _reader()

        reader.recurring_groups()

        bound = fake.parameters(catalog.RECURRING_GROUPS)
        assert (bound["min_size"], bound["min_messages"]) == (0, 0)

    @pytest.mark.parametrize("direction", list(TemplateDirection))
    def test_the_templates_bind_the_direction_as_its_string_value(
        self, direction: TemplateDirection
    ) -> None:
        """A raw statement's parameters reach the driver unconverted, which is
        the same reason ``as_graph_datetime`` exists.

        Both directions, because ``SENT.value`` *is* the string a hard-coded
        binding would produce: the one test written to pin this decision could
        not tell the two apart while it only ever asked for one of them.
        """
        fake, reader = _reader()

        reader.templates(direction, limit=10)

        assert fake.parameters(catalog.TOP_TEMPLATES) == {
            "direction": direction.value,
            "limit": 10,
        }

    def test_a_limit_of_zero_still_asks_for_a_row(self) -> None:
        """``LIMIT 0`` is legal Cypher that returns nothing, so a caller's
        stray zero would render as an empty archive rather than as the mistake
        it is."""
        fake, reader = _reader()

        reader.topics(limit=0)

        assert fake.parameters(catalog.TOPIC_BREAKDOWN) == {"limit": 1}

    def test_a_limit_nobody_could_read_is_capped(self) -> None:
        """The other end, and the one a caller outside this repository picks.

        ``AnalyticsReader`` is exported from ``mailarc_analytics`` and phase
        6's MCP server serves a model from these constants, so ``limit`` is an
        argument something else chooses. Ten million rows of a self-join, into
        two tuples and two dicts, beside an in-process graph, is an archive
        dump wearing a report's name.
        """
        fake, reader = _reader()

        reader.co_recipients(limit=10_000_000)

        assert fake.parameters(catalog.CO_RECIPIENTS) == {"limit": reports.MAX_ROWS}


class TestWhatEachListingDecodes:
    """Columns onto fields, and what a sparse graph turns into."""

    def test_a_ground_truth_pair_keeps_both_addresses_and_the_count(self) -> None:
        _fake, reader = _reader(
            {
                catalog.CO_RECIPIENTS: Reply(
                    columns=["left_id", "right_id", "together"],
                    rows=[["anna", "thomas", 5]],
                )
            }
        )

        found = reader.co_recipients()

        assert found[0].left_id == "anna"
        assert found[0].right_id == "thomas"
        assert found[0].together == 5

    def test_a_stored_pair_brings_its_span_back_aware(self) -> None:
        _fake, reader = _reader(
            {
                catalog.TOP_CO_ADDRESSED: Reply(
                    columns=[
                        "left_id",
                        "right_id",
                        "together",
                        "first_seen",
                        "last_seen",
                    ],
                    rows=[["anna", "thomas", 5, WHEN, LATER]],
                )
            }
        )

        found = reader.top_co_addressed()

        assert found[0].first_seen == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        assert found[0].last_seen == datetime(2026, 2, 10, 16, 0, tzinfo=UTC)

    def test_an_edge_whose_count_was_never_set_reads_as_zero(self) -> None:
        """Not skipped and not ``None``: a pair the write path left without a
        count is exactly what the cross-check should be able to see."""
        _fake, reader = _reader(
            {
                catalog.TOP_CO_ADDRESSED: Reply(
                    columns=[
                        "left_id",
                        "right_id",
                        "together",
                        "first_seen",
                        "last_seen",
                    ],
                    rows=[["anna", "thomas", None, None, None]],
                )
            }
        )

        found = reader.top_co_addressed()

        assert found[0].together == 0
        assert found[0].first_seen is None

    def test_a_group_row_carries_its_size_and_its_span(self) -> None:
        _fake, reader = _reader(
            {
                catalog.RECURRING_GROUPS: Reply(
                    columns=["id", "size", "message_count", "first_seen", "last_seen"],
                    rows=[["circle", 3, 5, WHEN, LATER]],
                )
            }
        )

        found = reader.recurring_groups()

        assert found[0].id == "circle"
        assert (found[0].size, found[0].message_count) == (3, 5)
        assert found[0].last_seen == datetime(2026, 2, 10, 16, 0, tzinfo=UTC)

    def test_a_topic_row_keeps_the_method_off_the_edge(self) -> None:
        _fake, reader = _reader(
            {
                catalog.TOPIC_BREAKDOWN: Reply(
                    columns=["id", "label", "method", "messages"],
                    rows=[["topic:1", "angebot", "ref", 5]],
                )
            }
        )

        found = reader.topics()

        assert (found[0].method, found[0].messages) == ("ref", 5)

    def test_a_topic_with_no_label_reads_as_an_empty_one(self) -> None:
        _fake, reader = _reader(
            {
                catalog.TOPIC_BREAKDOWN: Reply(
                    columns=["id", "label", "method", "messages"],
                    rows=[["topic:1", None, "ref", 5]],
                )
            }
        )

        assert reader.topics()[0].label == ""

    def test_every_template_row_is_stamped_with_the_direction_asked_for(
        self,
    ) -> None:
        """The statement filters on the direction and therefore never returns
        it, and the two directions are shown side by side."""
        _fake, reader = _reader(
            {
                catalog.TOP_TEMPLATES: Reply(
                    columns=[
                        "id",
                        "occurrences",
                        "automation_score",
                        "sample_text",
                        "first_seen",
                        "last_seen",
                    ],
                    rows=[
                        ["template:1e16:received", 10, 0.279724, "Hallo", WHEN, LATER]
                    ],
                )
            }
        )

        found = reader.templates(TemplateDirection.RECEIVED)

        assert found[0].direction is TemplateDirection.RECEIVED
        assert found[0].occurrences == 10
        assert found[0].automation_score == 0.279724
        assert found[0].sample_text == "Hallo"


class TestWhatTheCrossCheckAsks:
    """Two statements, back to back, in one session."""

    def test_both_sides_are_read_before_anything_is_compared(self) -> None:
        fake, reader = _reader()

        reader.co_addressed_agreement(limit=25)

        assert fake.statements == [catalog.CO_RECIPIENTS, catalog.TOP_CO_ADDRESSED]
        assert fake.opened == 1

    def test_both_sides_are_asked_for_the_same_number_of_rows(self) -> None:
        """The verdict's whole argument turns on where each listing was cut, so
        the two limits have to be one number."""
        fake, reader = _reader()

        reader.co_addressed_agreement(limit=25)

        assert fake.parameters(catalog.CO_RECIPIENTS) == {"limit": 25}
        assert fake.parameters(catalog.TOP_CO_ADDRESSED) == {"limit": 25}

    def test_it_asks_for_far_more_than_a_listing_by_default(self) -> None:
        """Nobody reads these rows one by one — the limit is how much of the
        archive the verdict is about."""
        fake, reader = _reader()

        reader.co_addressed_agreement()

        assert fake.parameters(catalog.CO_RECIPIENTS) == {"limit": AGREEMENT_LIMIT}
        assert AGREEMENT_LIMIT > reports.REPORT_LIMIT

    def test_a_matching_pair_of_listings_comes_back_agreeing(self) -> None:
        _fake, reader = _reader(
            {
                catalog.CO_RECIPIENTS: Reply(
                    columns=["left_id", "right_id", "together"],
                    rows=[["anna", "thomas", 5]],
                ),
                catalog.TOP_CO_ADDRESSED: Reply(
                    columns=[
                        "left_id",
                        "right_id",
                        "together",
                        "first_seen",
                        "last_seen",
                    ],
                    rows=[["anna", "thomas", 5, WHEN, LATER]],
                ),
            }
        )

        found = reader.co_addressed_agreement()

        assert found.agrees
        assert found.limit == AGREEMENT_LIMIT

    def test_an_edge_that_overstates_is_logged_as_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing legitimate makes the edge see *more* of the archive than the
        ground truth, so this is the one direction that gets a warning."""
        _fake, reader = _reader(
            {
                catalog.TOP_CO_ADDRESSED: Reply(
                    columns=[
                        "left_id",
                        "right_id",
                        "together",
                        "first_seen",
                        "last_seen",
                    ],
                    rows=[["anna", "revision", 3, WHEN, LATER]],
                )
            }
        )

        with caplog.at_level("WARNING"):
            found = reader.co_addressed_agreement()

        assert len(found.edge_overstates) == 1
        assert "does not support" in caplog.text


class TestThatNoCallerCanHandItCypher:
    """The catalogue's rule, applied to the module whose job is to run
    statements on behalf of a page — and read off the source, because a claim
    about how a string was built is invisible once it is a ``str``."""

    def test_no_value_in_the_facade_is_a_statement(self) -> None:
        """Docstrings excepted: a bare string expression is documentation, and
        everything else is a value the code actually uses."""
        found = [
            one
            for one in _values(_source(reports))
            if isinstance(one, str) and _CYPHER.search(one)
        ]

        assert found == []

    def test_every_statement_it_names_is_one_the_catalogue_owns(self) -> None:
        """``catalog.SOMETHING`` and nothing else, and the something is a
        statement rather than a helper that happens to live there."""
        named = {
            node.attr
            for node in ast.walk(_source(reports))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "catalog"
        }

        assert named
        assert named <= set(CATALOG)

    def test_the_facade_imports_from_a_cold_interpreter(self) -> None:
        """``queries`` sits *below* ``derived`` for the statements and *above*
        it for the rows, so the two packages import each other. Every ordering
        resolves today; a subprocess is the only place that can still be true
        after pytest has already imported half the tree.
        """
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "import mailarc_analytics.queries.reports"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert done.returncode == 0, done.stderr


def _source(module: ModuleType) -> ast.Module:
    return ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))


def _values(tree: ast.Module) -> list[object]:
    """Every constant the module *uses*, with its documentation left out.

    A docstring is a bare string statement — the module's own, a class's, a
    function's, or the one written under an attribute — so excluding every
    string that stands alone as an expression leaves exactly the constants
    something reads.
    """
    documented = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and id(node) not in documented
    ]
