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
from datetime import UTC, date, datetime
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
    a list of lists, with no entity mapping anywhere near it.

    Still written as a header plus rows although
    :func:`~mailarc_analytics.queries.rows.rows_of` now answers with dicts: the
    header is what the store really sends, the zip is what the session really
    does, and describing an answer column by column is what makes a test row
    readable. :meth:`Scripted.all_rows` does the zip, exactly where runic does
    it.
    """

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

    def __init__(self, answers: Mapping[Any, Reply] | None = None) -> None:
        self._answers = dict(answers or {})
        self.asked: list[tuple[Any, dict[str, Any]]] = []
        self.opened = 0

    def all_rows(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """The one member the façade reaches for, and the shape runic answers in.

        ``all_rows`` and not ``execute``: a catalogue statement is a
        query-builder object that only the session can bind — handing one to
        the driver raises ``TypeError: can only concatenate str`` — so this is
        the call every read in ``reports.py`` makes, and modelling anything
        else would let a test pass over a call the store would refuse.
        """
        self.asked.append((statement, dict(params or {})))
        reply = self._answers.get(statement, Reply())
        return [dict(zip(reply.columns, row, strict=True)) for row in reply.rows]

    @contextmanager
    def open(self) -> Iterator[Session]:
        self.opened += 1
        yield cast(Session, self)

    @property
    def statements(self) -> list[Any]:
        return [statement for statement, _ in self.asked]

    def parameters(self, statement: Any) -> dict[str, Any]:
        """What the one call to *statement* bound."""
        return next(params for asked, params in self.asked if asked is statement)


def _reader(
    answers: Mapping[Any, Reply] | None = None,
) -> tuple[Scripted, AnalyticsReader]:
    fake = Scripted(answers)
    return fake, AnalyticsReader(fake.open)


def _total(value: int) -> Reply:
    return Reply(columns=["total"], rows=[[value]])


def _days(*rows: tuple[str, int, float]) -> Reply:
    """``ARCHIVED_PER_DAY``'s answer, newest first the way the statement orders
    it — so a test that passes only because the reader was handed its rows in
    calendar order fails here instead."""
    return Reply(
        columns=["day", "messages", "bytes"],
        rows=[[day, messages, size] for day, messages, size in reversed(rows)],
    )


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


class TestWhatPhaseTwoAsksFor:
    """The five listings the analysis stages made answerable.

    Same two claims as the four above them — which statement, with what bound,
    and what each column becomes — for the readings that did not exist before
    there were circles, scores, keywords and suggestions to read.
    """

    def test_the_circles_bind_only_a_limit(self) -> None:
        fake, reader = _reader()

        reader.communities(limit=10)

        assert fake.statements == [catalog.TOP_COMMUNITIES]
        assert fake.parameters(catalog.TOP_COMMUNITIES) == {"limit": 10}

    def test_a_circle_row_carries_its_label_its_size_and_its_span(self) -> None:
        _fake, reader = _reader(
            {
                catalog.TOP_COMMUNITIES: Reply(
                    columns=[
                        "id",
                        "label",
                        "size",
                        "message_count",
                        "method",
                        "first_seen",
                        "last_seen",
                    ],
                    rows=[["community:1", "kunde.example", 4, 12, "lpa", WHEN, LATER]],
                )
            }
        )

        found = reader.communities()

        assert found[0].id == "community:1"
        assert found[0].label == "kunde.example"
        assert (found[0].size, found[0].message_count) == (4, 12)
        assert found[0].method == "lpa"
        assert found[0].last_seen == datetime(2026, 2, 10, 16, 0, tzinfo=UTC)

    def test_the_important_messages_bind_only_a_limit(self) -> None:
        fake, reader = _reader()

        reader.important_messages(limit=10)

        assert fake.parameters(catalog.TOP_IMPORTANT) == {"limit": 10}

    def test_an_important_message_keeps_its_score_and_every_reason(self) -> None:
        """The reasons are the point: a ranking a user cannot argue with is a
        ranking a user cannot correct."""
        _fake, reader = _reader(
            {
                catalog.TOP_IMPORTANT: Reply(
                    columns=[
                        "id",
                        "subject",
                        "sent_at",
                        "sender",
                        "importance",
                        "reasons",
                    ],
                    rows=[
                        [
                            "m1@nordlicht.example",
                            "Angebot",
                            WHEN,
                            "anna@kunde.example",
                            0.65,
                            ["addressed directly", "replied by you"],
                        ]
                    ],
                )
            }
        )

        found = reader.important_messages()

        assert found[0].id == "m1@nordlicht.example"
        assert found[0].sender == "anna@kunde.example"
        assert found[0].importance == 0.65
        assert found[0].reasons == ("addressed directly", "replied by you")
        assert found[0].sent_at == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)

    def test_a_message_whose_sender_never_arrived_still_ranks(self) -> None:
        """The sender hop is optional, because a broken import should show the
        score and the gap rather than hide both."""
        _fake, reader = _reader(
            {
                catalog.TOP_IMPORTANT: Reply(
                    columns=[
                        "id",
                        "subject",
                        "sent_at",
                        "sender",
                        "importance",
                        "reasons",
                    ],
                    rows=[["m1", None, None, None, 0.4, None]],
                )
            }
        )

        found = reader.important_messages()

        assert (found[0].sender, found[0].subject) == ("", "")
        assert found[0].reasons == ()
        assert found[0].sent_at is None

    def test_the_topic_keywords_bind_only_a_limit(self) -> None:
        fake, reader = _reader()

        reader.topic_keywords(limit=10)

        assert fake.parameters(catalog.TOPIC_KEYWORDS) == {"limit": 10}

    def test_a_keyword_row_keeps_the_words_in_the_order_they_were_ranked(
        self,
    ) -> None:
        """TF-IDF put the most discriminating term first, and a set or a sorted
        tuple here would throw that away."""
        _fake, reader = _reader(
            {
                catalog.TOPIC_KEYWORDS: Reply(
                    columns=["id", "label", "keywords", "message_count"],
                    rows=[["topic:1", "angebot", ["migration", "angebot"], 5]],
                )
            }
        )

        found = reader.topic_keywords()

        assert found[0].keywords == ("migration", "angebot")
        assert found[0].message_count == 5

    def test_the_suggestion_counts_bind_nothing_at_all(self) -> None:
        """Every tag, unlimited: the population is the list of names a person
        made, which is a list somebody is already looking at."""
        fake, reader = _reader()

        reader.suggestion_counts()

        assert fake.parameters(catalog.SUGGESTION_COUNTS) == {}

    def test_a_tag_with_nothing_to_accept_comes_back_as_a_zero(self) -> None:
        """Not absent. A tag with no suggestions and a tag missing from a
        listing look the same to a card, and only one of them is a state a user
        should be shown."""
        _fake, reader = _reader(
            {
                catalog.SUGGESTION_COUNTS: Reply(
                    columns=["id", "name", "suggestions"],
                    rows=[["tag:a", "Alpha", 3], ["tag:b", "Beta", 0]],
                )
            }
        )

        assert reader.suggestion_counts() == {"tag:a": 3, "tag:b": 0}

    def test_one_tags_suggestions_bind_the_tag_and_a_limit(self) -> None:
        fake, reader = _reader()

        reader.suggestions_for("tag:nord-42", limit=10)

        assert fake.parameters(catalog.TAG_SUGGESTIONS) == {
            "tag": "tag:nord-42",
            "limit": 10,
        }

    def test_a_page_s_topics_bind_its_ids_once(self) -> None:
        fake, reader = _reader()

        reader.topics_of(["m1", "m2", "m1"])

        assert fake.parameters(catalog.TOPICS_OF_MESSAGES) == {"ids": ["m1", "m2"]}
        assert fake.opened == 1

    def test_a_topic_membership_names_the_topic_and_its_words(self) -> None:
        _fake, reader = _reader(
            {
                catalog.TOPICS_OF_MESSAGES: Reply(
                    columns=["message_id", "topic_id", "label", "keywords"],
                    rows=[["m1", "topic:8f3", "Angebot", ["angebot", "q3"]]],
                )
            }
        )

        found = reader.topics_of(["m1", "m2"])

        assert list(found) == ["m1"]
        assert found["m1"].topic_id == "topic:8f3"
        assert found["m1"].label == "Angebot"
        assert found["m1"].keywords == ("angebot", "q3")

    def test_a_message_filed_twice_resolves_to_the_smallest_topic(self) -> None:
        """Whatever order the rows came back in."""
        _fake, reader = _reader(
            {
                catalog.TOPICS_OF_MESSAGES: Reply(
                    columns=["message_id", "topic_id", "label", "keywords"],
                    rows=[
                        ["m1", "topic:b", "", None],
                        ["m1", "topic:a", "", None],
                    ],
                )
            }
        )

        assert reader.topics_of(["m1"])["m1"].topic_id == "topic:a"

    def test_no_ids_ask_for_no_topics(self) -> None:
        fake, reader = _reader()

        assert reader.topics_of([]) == {}
        assert fake.opened == 0

    def test_a_page_s_groups_bind_its_ids_once(self) -> None:
        fake, reader = _reader()

        reader.groups_of(["m1", "m2"])

        assert fake.parameters(catalog.GROUPS_OF_MESSAGES) == {"ids": ["m1", "m2"]}
        assert fake.opened == 1

    def test_a_group_membership_carries_the_group_s_size(self) -> None:
        _fake, reader = _reader(
            {
                catalog.GROUPS_OF_MESSAGES: Reply(
                    columns=["message_id", "group_id", "size", "message_count"],
                    rows=[["m1", "group:9c", 5, 40], ["m1", "group:1a", 3, 4]],
                )
            }
        )

        found = reader.groups_of(["m1"])

        assert found["m1"].group_id == "group:1a"
        assert (found["m1"].size, found["m1"].message_count) == (3, 4)

    def test_no_ids_ask_for_no_groups(self) -> None:
        fake, reader = _reader()

        assert reader.groups_of([]) == {}
        assert fake.opened == 0

    def test_a_suggestion_row_carries_the_case_that_was_made_for_it(self) -> None:
        """``score`` and ``method`` both, because "these two share a thread" and
        "these two are in the same circle" are not the same claim."""
        _fake, reader = _reader(
            {
                catalog.TAG_SUGGESTIONS: Reply(
                    columns=["id", "subject", "sent_at", "score", "method"],
                    rows=[["m3", "Re: Angebot", WHEN, 0.36, "thread"]],
                )
            }
        )

        found = reader.suggestions_for("tag:nord-42")

        assert found[0].message_id == "m3"
        assert found[0].subject == "Re: Angebot"
        assert (found[0].score, found[0].method) == (0.36, "thread")

    def test_every_new_listing_is_capped_like_the_old_ones(self) -> None:
        """``AnalyticsReader`` is exported and phase 6 serves a model from it,
        so ``limit`` is an argument something outside this repository picks."""
        fake, reader = _reader()

        reader.communities(limit=0)
        reader.important_messages(limit=10_000_000)
        reader.suggestions_for("tag:a", limit=0)

        assert fake.parameters(catalog.TOP_COMMUNITIES) == {"limit": 1}
        assert fake.parameters(catalog.TOP_IMPORTANT) == {"limit": reports.MAX_ROWS}
        assert fake.parameters(catalog.TAG_SUGGESTIONS)["limit"] == 1

    def test_each_listing_opens_one_session(self) -> None:
        """A read is complete in itself; five listings are five questions."""
        fake, reader = _reader()

        reader.communities()

        assert fake.opened == 1


class TestTheArchivingHistory:
    """One statement, and the two things the reader does to its answer.

    The window and the gap-filling are the whole method: the statement returns
    the days on which something was archived, in an order and under a ceiling,
    and a chart needs a fixed number of consecutive days ending today. So the
    day the window ends on is pinned here rather than taken from the clock —
    :func:`~mailarc_analytics.queries.reports._today` is the seam that exists
    for it, and a test that read the clock too would agree with the reader by
    construction and fail once a year at midnight.
    """

    def test_the_row_ceiling_leaves_room_for_days_stamped_in_the_future(
        self,
    ) -> None:
        """One day is one row — but not only days inside the window have rows.

        The statement orders by day descending and stops at its ceiling, so a
        stamp from a machine whose clock ran ahead, or from a restored backup,
        is returned *first* and spends a slot the window wanted. Bound to the
        window itself, the ceiling was spent on days nobody asked for and the
        oldest real ones were gap-filled as zeros. So the ceiling is wider than
        the window while the window stays what was asked for.
        """
        fake, reader = _reader()

        found = reader.archived_per_day(days=7)

        assert fake.statements == [catalog.ARCHIVED_PER_DAY]
        assert fake.parameters(catalog.ARCHIVED_PER_DAY) == {"limit": 14}
        assert len(found) == 7
        assert fake.opened == 1

    def test_a_window_of_no_days_still_asks_for_one(self) -> None:
        """``LIMIT 0`` is legal Cypher that returns nothing, and a chart of
        nought days is not a state anything wants to render."""
        fake, reader = _reader()

        found = reader.archived_per_day(days=0)

        assert fake.parameters(catalog.ARCHIVED_PER_DAY) == {"limit": 2}
        assert len(found) == 1

    def test_a_window_nobody_could_read_is_capped_at_the_same_ceiling(self) -> None:
        """Thirteen years of daily rows is the ceiling ``MAX_ROWS`` already
        names, and the cap has to move the *window* and not only the binding —
        a window wider than the rows asked for would be filled with zeros the
        statement was never given a chance to contradict.

        ``MAX_ROWS`` is where the two numbers meet again: the room the ceiling
        normally keeps for future-stamped rows cannot be bought above it.
        """
        fake, reader = _reader()

        found = reader.archived_per_day(days=10_000_000)

        assert fake.parameters(catalog.ARCHIVED_PER_DAY) == {"limit": reports.MAX_ROWS}
        assert len(found) == reports.MAX_ROWS

    def test_the_window_is_a_run_of_days_ending_today(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consecutive and ascending, whatever the statement returned — the
        chart's x-axis is a calendar, not a list of days something happened."""
        _fake, reader = _reader({catalog.ARCHIVED_PER_DAY: _days(("2026-03-04", 2, 9))})
        monkeypatch.setattr(reports, "_today", lambda: date(2026, 3, 5))

        found = reader.archived_per_day(days=3)

        assert [one.day for one in found] == ["2026-03-03", "2026-03-04", "2026-03-05"]

    def test_a_day_the_statement_never_returned_is_a_zero_and_not_a_hole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chart with a hole in it reads as missing data rather than as a
        quiet day, which is the opposite of what an idle archive means."""
        _fake, reader = _reader({catalog.ARCHIVED_PER_DAY: _days(("2026-03-05", 2, 9))})
        monkeypatch.setattr(reports, "_today", lambda: date(2026, 3, 5))

        found = reader.archived_per_day(days=3)

        assert [(one.messages, one.bytes) for one in found] == [(0, 0), (0, 0), (2, 9)]

    def test_a_day_older_than_the_window_is_cut_rather_than_folded_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ceiling is a number of rows and the window is a span of days, so
        an archive that was idle for a month hands back rows from before it.
        Keeping them would stretch the axis; adding them to the first day would
        invent a spike."""
        _fake, reader = _reader(
            {
                catalog.ARCHIVED_PER_DAY: _days(
                    ("2025-11-02", 40, 4000), ("2026-03-05", 2, 9)
                )
            }
        )
        monkeypatch.setattr(reports, "_today", lambda: date(2026, 3, 5))

        found = reader.archived_per_day(days=3)

        assert len(found) == 3
        assert sum(one.messages for one in found) == 2

    def test_a_day_after_today_is_cut_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``archived_at`` is a wall clock somebody else set. A machine whose
        clock ran ahead — or a restored backup — leaves a stamp in the future,
        and a window that ends today has to end today."""
        _fake, reader = _reader(
            {catalog.ARCHIVED_PER_DAY: _days(("2026-03-09", 5, 50))}
        )
        monkeypatch.setattr(reports, "_today", lambda: date(2026, 3, 5))

        found = reader.archived_per_day(days=2)

        assert [one.day for one in found] == ["2026-03-04", "2026-03-05"]
        assert sum(one.messages for one in found) == 0

    def test_the_summed_bytes_come_back_as_a_whole_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured on the vendored FalkorDB: ``sum()`` answers with a float,
        so ``bytes`` arrives as ``3139.0`` and a page would render it."""
        _fake, reader = _reader(
            {catalog.ARCHIVED_PER_DAY: _days(("2026-03-05", 2, 3139.0))}
        )
        monkeypatch.setattr(reports, "_today", lambda: date(2026, 3, 5))

        found = reader.archived_per_day(days=1)

        assert found[0].bytes == 3139
        assert isinstance(found[0].bytes, int)

    def test_a_day_key_the_store_could_not_have_written_costs_one_row(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``left()`` cuts ten characters off whatever the property holds, so a
        stamp written by something other than the writer comes back as a key no
        calendar has. One row loses its day; a report that died over it would
        lose the whole chart.
        """
        _fake, reader = _reader(
            {
                catalog.ARCHIVED_PER_DAY: _days(
                    ("not-a-day", 7, 70), ("2026-03-05", 2, 9)
                )
            }
        )
        monkeypatch.setattr(reports, "_today", lambda: date(2026, 3, 5))

        with caplog.at_level("WARNING"):
            found = reader.archived_per_day(days=1)

        assert [(one.day, one.messages) for one in found] == [("2026-03-05", 2)]
        assert "not-a-day" in caplog.text


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
