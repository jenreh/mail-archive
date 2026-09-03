"""Tests for what :mod:`mailarc_ui.insights.state` reads and how it projects it.

Against the real :class:`AnalyticsReader` over the fake session in
:mod:`insights_archive`; the rebuild control and its poll are next door in
:mod:`test_ui_insights_rebuild`.

Three claims are worth proving here. That the state finds its reader where the
composition root left it. That an archive nobody has rebuilt says what to do
rather than showing four empty tables — and, more to the point, that it does
not run the self-join to find that out. And that the cross-check's verdict
turns red for an edge that claims *more* than the archive supports and only
yellow for one that is merely behind it, because those two are the difference
between a bug in the write path and a rebuild that has not been run yet.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest import mock

import pytest
import reflex as rx
from appkit_commons.registry import service_registry
from insights_archive import (
    AUGUST,
    COUNTS,
    MARCH,
    fresh,
    graph,
    pairs,
    published,
    tags,
)
from pydantic import ValidationError

from mailarc_analytics import (
    AnalyticsReader,
    CoAddressedAgreement,
    CoAddressedRow,
    CoRecipientRow,
    TemplateDirection,
)
from mailarc_analytics.derived.model import TopicSignal
from mailarc_analytics.queries import catalog
from mailarc_ui.insights import (
    AgreementView,
    AnalyticsInsightsState,
    GroupView,
    PairView,
    agreement_card,
    analyses,
    analytics_reader,
    communities_card,
    disputes_table,
    groups_card,
    guidance_panel,
    important_card,
    insights_panel,
    method_color,
    pairs_table,
    rebuild_card,
    rebuild_controls,
    short_key,
    span_label,
    tags_card,
    templates_card,
    topics_card,
    totals_card,
)
from mailarc_ui.insights.model import (
    DISPUTE_LIMIT,
    METHOD_COLORS,
    sample_label,
    short_date,
)

__all__ = ["fresh", "graph", "published", "tags"]
"""pytest collects a fixture off the importing module's namespace, so the four
are imported to be used; ``__all__`` is what stops ruff removing them again."""


@pytest.fixture
def state(published: AnalyticsReader) -> AnalyticsInsightsState:
    """The state under test, over the published reader."""
    return AnalyticsInsightsState()


def _truth_row(left: str, right: str, together: int) -> CoRecipientRow:
    """One ``CO_RECIPIENTS`` row: the ground truth's side of the comparison."""
    return CoRecipientRow(
        left_id=f"{left}@example.com",
        right_id=f"{right}@example.com",
        together=together,
    )


def _edge_row(left: str, right: str, together: int) -> CoAddressedRow:
    """One ``TOP_CO_ADDRESSED`` row: what the materialised edge carries."""
    return CoAddressedRow(
        left_id=f"{left}@example.com",
        right_id=f"{right}@example.com",
        together=together,
    )


async def _load(state: AnalyticsInsightsState) -> None:
    """Invoke the page's ``on_load`` the way Reflex invokes a background task.

    ``load`` reads the whole archive and holds the state lock only around its
    two mutations, so Reflex refuses a direct call on it; going through the
    ``EventHandler``'s wrapped function is the same code the app runs.
    """
    await AnalyticsInsightsState.load.fn(state)


async def _check_agreement(state: AnalyticsInsightsState) -> None:
    """The Cross-check button, same reason as :func:`_load`."""
    await AnalyticsInsightsState.check_agreement.fn(state)


class TestFindingTheReader:
    def test_the_published_reader_is_the_one_the_state_uses(self, published) -> None:
        assert analytics_reader() is published

    def test_an_unpublished_reader_is_a_sentence_not_a_key_error(self) -> None:
        registry = service_registry()
        saved = registry.snapshot()
        try:
            registry.restore({})
            with pytest.raises(RuntimeError, match=r"app\.composition"):
                analytics_reader()
        finally:
            registry.restore(saved)

    async def test_a_page_without_a_reader_shows_the_sentence(self, graph) -> None:
        registry = service_registry()
        saved = registry.snapshot()
        try:
            registry.restore({})
            state = AnalyticsInsightsState()

            await _load(state)

            assert "app.composition" in state.totals_error
            assert state.busy is False
            assert graph.asked == []
        finally:
            registry.restore(saved)


class TestTheFirstLook:
    """A fresh archive is the first thing anybody sees on this page."""

    async def test_nothing_derived_says_what_to_do(self, state, fresh) -> None:
        await _load(state)

        assert state.needs_rebuild is True
        assert state.totals.messages == 12
        assert state.totals.derived == 0
        assert "12 messages are archived" in state.guidance
        assert "rebuild" in state.guidance.lower()

    async def test_nothing_derived_does_not_run_the_self_join(
        self, state, fresh
    ) -> None:
        """Four empty tables would be the small waste; the cross-check over an
        empty edge would report the whole archive as a disagreement."""
        await _load(state)

        assert set(fresh.asked) == set(COUNTS)
        assert state.pairs == []
        assert state.groups == []
        assert state.agreement.headline == ""

    async def test_an_empty_archive_asks_for_an_import_not_a_rebuild(
        self, state, graph
    ) -> None:
        graph.count(catalog.COUNT_MESSAGES, 0)
        for statement in (
            catalog.COUNT_GROUPS,
            catalog.COUNT_TOPICS,
            catalog.COUNT_TEMPLATES,
            catalog.COUNT_CO_ADDRESSED,
        ):
            graph.count(statement, 0)

        await _load(state)

        assert state.has_archive is False
        assert state.needs_rebuild is False
        assert "Import a mailbox" in state.guidance

    def test_nothing_is_claimed_before_the_first_read_returns(self, state) -> None:
        """Every panel starts as "has not answered yet" — an empty table would
        be a claim about the archive that nothing has earned."""
        assert state.busy is True
        assert state.loading_totals is True
        assert state.guidance == ""

    async def test_a_full_archive_has_nothing_to_say_instead(self, state) -> None:
        await _load(state)

        assert state.guidance == ""
        assert state.needs_rebuild is False


class TestReadingTheAnalyses:
    async def test_the_totals_are_ground_truth_and_derived_together(
        self, state
    ) -> None:
        await _load(state)

        assert state.totals.messages == 12
        assert state.totals.unidentified == 1
        assert state.totals.co_addressed == 3
        assert state.totals.derived == 2 + 2 + 2 + 3
        assert state.totals_error == ""
        assert state.busy is False

    async def test_a_group_is_the_row_the_catalogue_answered_made_printable(
        self, state
    ) -> None:
        await _load(state)

        first, second = state.groups
        assert first.key == "a" * 12 + "…"
        assert first.size == 4
        assert first.message_count == 9
        assert first.span == (
            f"{MARCH.astimezone():%d.%m.%y} – {AUGUST.astimezone():%d.%m.%y}"
        )
        assert second.span == ""
        assert state.groups_error == ""

    async def test_a_topic_wears_its_signal_as_a_colour(self, state) -> None:
        """§6.2: the method is the difference between a fact and a
        suggestion, so it has to be visible without clicking."""
        await _load(state)

        strong, weak = state.topics
        assert strong.label == "rechnung swiftscan"
        assert strong.method == "ref"
        assert strong.method_color == "teal"
        assert strong.messages == 6
        assert weak.label == "(no subject in common)"
        assert weak.method == "participants"
        assert weak.method_color == "orange"

    async def test_a_topic_with_no_method_says_unknown_rather_than_nothing(
        self, state, graph
    ) -> None:
        """The sibling fallback on the same row is asserted; this one was not,
        so ``method=row.method or "unknown"`` could be hard-coded to any
        constant and the whole suite stayed green — with the badge §6.2 calls
        the fact-versus-suggestion column mislabelling every topic."""
        graph.rows(
            catalog.TOPIC_BREAKDOWN,
            ["id", "label", "method", "messages"],
            [["topic:" + "e" * 32, "invoice", "", 2]],
        )

        await _load(state)

        assert [(one.method, one.method_color) for one in state.topics] == [
            ("unknown", "gray")
        ]

    async def test_templates_come_back_one_direction_at_a_time(
        self, state, graph
    ) -> None:
        """§6.3: only what you write yourself is automatable, and the scores
        are meaningless across the two."""
        await _load(state)

        sent = state.sent_templates[0]
        assert sent.key == "1a2b3c4d5e6f…"
        assert sent.occurrences == 7
        assert sent.score == 72.0
        assert sent.score_label == "0.72"
        assert sent.sample == "Sehr geehrte Damen und Herren, anbei die Rechnung."
        assert [one.score_label for one in state.received_templates] == ["0.31"]
        assert state.has_templates is True
        directions = {
            one["direction"]
            for statement, one in zip(graph.asked, graph.params, strict=True)
            if statement == catalog.TOP_TEMPLATES
        }
        assert directions == {"sent", "received"}

    async def test_one_direction_alone_still_fills_the_panel(
        self, state, graph
    ) -> None:
        """ "Either direction found something — one panel holds both" is the
        mixed case, and only the two uniform ones were ever constructed. A
        mailbox that is mostly read and rarely written has no sent templates
        above the threshold and several received ones, and narrowing
        ``has_templates`` to one side would render the empty-state sentence
        over a non-empty listing."""
        graph.template_rows(TemplateDirection.SENT, [])

        await _load(state)

        assert state.sent_templates == []
        assert len(state.received_templates) == 1
        assert state.has_templates is True

    async def test_the_other_direction_alone_does_too(self, state, graph) -> None:
        """The mirror, so neither side can be the one that carries the var."""
        graph.template_rows(TemplateDirection.RECEIVED, [])

        await _load(state)

        assert state.received_templates == []
        assert len(state.sent_templates) == 1
        assert state.has_templates is True


class TestTheCrossCheck:
    """The panel that makes this page a test rather than a report."""

    async def test_an_edge_that_matches_the_archive_reads_as_agreement(
        self, state
    ) -> None:
        await _load(state)

        assert state.agreement.agrees is True
        assert state.agreement.color == "teal"
        assert state.agreement.matched == 3
        assert state.agreement.disputes == []
        assert state.agreement.overstated == 0
        assert "agree on all 3 pairs" in state.agreement.headline
        assert state.agreement_error == ""

    async def test_the_pairs_the_edge_claims_are_shown_beside_the_verdict(
        self, state
    ) -> None:
        await _load(state)

        heaviest = state.pairs[0]
        assert heaviest.left_id == "anna@example.com"
        assert heaviest.right_id == "bob@example.com"
        assert heaviest.together == 5
        assert heaviest.span.startswith(f"{MARCH.astimezone():%d.%m.%y}")

    async def test_an_edge_counting_more_than_the_archive_turns_red(
        self, state, graph
    ) -> None:
        """The one direction nothing legitimate produces: a stale, capped or
        wide-recipient rebuild all make the edge see *less*."""
        pairs(
            graph,
            [["anna@example.com", "bob@example.com", 5]],
            [["anna@example.com", "bob@example.com", 9, None, None]],
        )

        await _load(state)

        assert state.agreement.agrees is False
        assert state.agreement.color == "red"
        assert state.agreement.overstated == 1
        assert "counts more than the archive supports on 1 pair" in (
            state.agreement.headline
        )
        dispute = state.agreement.disputes[0]
        assert (dispute.truth, dispute.edge) == ("5", "9")
        assert dispute.overstated is True
        assert dispute.note_color == "red"
        assert dispute.note == "the edge counts more than the archive"

    async def test_a_pair_only_the_edge_has_is_the_same_kind_of_wrong(
        self, state, graph
    ) -> None:
        pairs(graph, [], [["anna@example.com", "bob@example.com", 4, None, None]])

        await _load(state)

        assert state.agreement.color == "red"
        assert state.agreement.edge_only == 1
        dispute = state.agreement.disputes[0]
        assert (dispute.truth, dispute.edge) == ("—", "4")
        assert dispute.note == "the archive has no such pair"
        assert dispute.overstated is True

    def test_a_pair_the_archive_may_simply_have_below_the_cut_says_so(self) -> None:
        """What the comparison proved, and not a word more.

        With the truth listing full, an edge-only pair standing above its floor
        proves the edge *overstates*. It does not prove the archive has never
        seen the pair — on a large archive the pair may sit just below the cut,
        and "only the edge has this pair" is a claim about the archive that
        nothing here established. A reader who greps for that pair and finds it
        stops believing the panel, which is the only thing this panel trades
        on.

        Built straight off the verdict rather than through the fake archive: it
        takes a full listing to have a non-zero floor, and 500 planted rows to
        say one sentence would be a test about arithmetic nobody doubts.
        """
        found = CoAddressedAgreement.between(
            [
                _truth_row("a", "z", 20),
                _truth_row("b", "z", 15),
                _truth_row("c", "z", 12),
            ],
            [_edge_row("only", "edge", 40)],
            limit=3,
        )

        view = AgreementView.from_agreement(found)

        assert found.truth_floor == 12
        assert view.color == "red"
        assert view.disputes[0].note == "the archive counts this pair at most 12"

    def test_a_pair_carrying_two_edges_is_red_and_named(self) -> None:
        """Neither excess, so the yellow branch would have claimed it.

        Both counts can even agree with the archive — what is wrong is that the
        graph holds two relationships where the writer can only produce one.
        Falling through to "the archive counts more than the edge" would have
        printed a sentence about staleness over a write-path bug.
        """
        found = CoAddressedAgreement.between(
            [_truth_row("a", "b", 2)],
            [_edge_row("a", "b", 2), _edge_row("a", "b", 2)],
            limit=500,
        )

        view = AgreementView.from_agreement(found)

        assert view.color == "red"
        assert "two CO_ADDRESSED edges" in view.headline
        assert view.duplicate_pairs == 1

    def test_a_pair_an_exhaustive_archive_really_lacks_says_that_instead(
        self,
    ) -> None:
        """A truth listing that came back short was never cut, so its silence
        is proof and the stronger sentence is the true one."""
        found = CoAddressedAgreement.between(
            [_truth_row("a", "z", 20)], [_edge_row("only", "edge", 40)], limit=500
        )

        view = AgreementView.from_agreement(found)

        assert found.truth_floor == 0
        assert view.disputes[0].note == "the archive has no such pair"

    async def test_an_edge_that_is_merely_behind_is_yellow(self, state, graph) -> None:
        """A rebuild that has not run since the last import looks exactly like
        this, and an alarm spent here is an alarm nobody reads next time."""
        pairs(
            graph,
            [["anna@example.com", "bob@example.com", 5]],
            [["anna@example.com", "bob@example.com", 2, None, None]],
        )

        await _load(state)

        assert state.agreement.color == "yellow"
        assert state.agreement.overstated == 0
        assert state.agreement.mismatched == 1
        assert "archive counts more than the edge on 1 pair" in (
            state.agreement.headline
        )
        assert state.agreement.disputes[0].note == "the edge is behind the archive"
        assert state.agreement.disputes[0].note_color == "yellow"

    async def test_a_pair_the_edge_never_wrote_is_reported_without_alarm(
        self, state, graph
    ) -> None:
        pairs(graph, [["anna@example.com", "bob@example.com", 4]], [])

        await _load(state)

        assert state.agreement.color == "yellow"
        assert state.agreement.truth_only == 1
        dispute = state.agreement.disputes[0]
        assert (dispute.truth, dispute.edge) == ("4", "—")
        assert dispute.note == "the edge never wrote this pair"
        assert dispute.overstated is False

    async def test_the_overstating_pairs_are_listed_first(self, state, graph) -> None:
        """A heavier innocent disagreement must not bury the one that matters."""
        pairs(
            graph,
            [
                ["anna@example.com", "bob@example.com", 90],
                ["carl@example.com", "dora@example.com", 2],
            ],
            [
                ["anna@example.com", "bob@example.com", 40, None, None],
                ["carl@example.com", "dora@example.com", 8, None, None],
            ],
        )

        await _load(state)

        first, second = state.agreement.disputes
        assert (first.left_id, first.overstated) == ("carl@example.com", True)
        assert (second.left_id, second.overstated) == ("anna@example.com", False)

    async def test_a_long_list_of_disagreements_is_cut_and_says_so(
        self, state, graph
    ) -> None:
        many = [[f"a{n:03d}@example.com", "bob@example.com", 30 - n] for n in range(25)]
        pairs(graph, many, [])

        await _load(state)

        assert state.agreement.truth_only == 25
        assert state.agreement.disputes_total == 25
        assert len(state.agreement.disputes) == DISPUTE_LIMIT
        assert f"heaviest {DISPUTE_LIMIT} of 25" in state.agreement.disputes_note

    async def test_the_coverage_says_what_was_left_open(self, state) -> None:
        await _load(state)

        assert "3 pairs judged" in state.agreement.coverage
        assert "0 left open" in state.agreement.coverage
        assert str(state.agreement.limit) in state.agreement.coverage

    async def test_rechecking_asks_a1_again_and_nothing_else(
        self, state, graph
    ) -> None:
        await _load(state)
        graph.asked.clear()

        await _check_agreement(state)

        assert set(graph.asked) == {catalog.CO_RECIPIENTS, catalog.TOP_CO_ADDRESSED}
        assert state.loading_agreement is False
        assert state.agreement.agrees is True

    async def test_rechecking_without_a_reader_is_a_sentence(self, state) -> None:
        registry = service_registry()
        saved = registry.snapshot()
        try:
            registry.restore({})
            await _check_agreement(state)
        finally:
            registry.restore(saved)

        assert "app.composition" in state.agreement_error
        assert state.loading_agreement is False


class TestWhenTheGraphIsAway:
    """An outage is a state, not an exception (§ graph.status)."""

    async def test_counts_that_do_not_answer_stop_the_rest(self, state, graph) -> None:
        graph.failing = {catalog.COUNT_MESSAGES}

        await _load(state)

        assert state.totals_error == "graph is down"
        assert state.guidance == ""
        assert catalog.CO_RECIPIENTS not in graph.asked
        assert state.busy is False

    async def test_one_panel_that_fails_leaves_the_others_standing(
        self, state, graph
    ) -> None:
        graph.failing = {catalog.TOPIC_BREAKDOWN}

        await _load(state)

        assert state.topics_error == "graph is down"
        assert state.topics == []
        assert state.groups_error == ""
        assert len(state.groups) == 2
        assert state.agreement.agrees is True

    async def test_a_failed_cross_check_does_not_read_as_agreement(
        self, state, graph
    ) -> None:
        graph.failing = {catalog.CO_RECIPIENTS}

        await _load(state)

        assert state.agreement_error == "graph is down"
        assert state.agreement.agrees is False
        assert state.agreement.headline == ""

    async def test_a_listing_that_dies_after_the_verdict_still_says_so(
        self, state, graph
    ) -> None:
        """``verdict_error or listing_error``: the second half was unreachable.

        The fake failed per *statement*, and ``TOP_CO_ADDRESSED`` runs inside
        ``co_addressed_agreement`` as well as for the listing — so anything
        that set ``listing_error`` set ``verdict_error`` first and dropping the
        operand entirely left the suite green. Branch coverage cannot see it
        either: the operands of a short-circuit are not branches, and the
        module reports 100%. In production the two are separate round trips,
        and a graph that goes away between them would have rendered a verdict
        over a silently empty table.
        """
        graph.failing_from = {catalog.TOP_CO_ADDRESSED: 2}

        await _load(state)

        assert state.agreement.agrees is True, "the verdict got its answer"
        assert state.pairs == []
        assert "went away" in state.agreement_error

    async def test_either_direction_of_templates_failing_is_one_sentence(
        self, state, graph
    ) -> None:
        graph.failing = {catalog.TOP_TEMPLATES}

        await _load(state)

        assert state.templates_error == "graph is down"
        assert state.has_templates is False


class TestADateTheLocalZoneCannotPrint:
    """One forgeable header must not be able to take the page down.

    ``Date: Fri, 31 Dec 9999 23:59:59 +0000`` parses — ``parsedate_to_datetime``
    accepts it and ``_sent_at`` range-checks nothing — and pinning a mail to
    the top of a date sort is a routine spam trick. It reaches the graph, comes
    back through the reader as an aware datetime, and then
    ``datetime(9999, ...).astimezone()`` raises ``OverflowError`` in every zone
    east of UTC, which is the developer's own. A year-0001 date does the same
    thing west of it.

    The zone is forced rather than assumed: on a UTC machine — CI — neither
    extreme overflows, so a test that trusted the ambient zone would prove
    nothing exactly where it runs most often.
    """

    @pytest.fixture(autouse=True)
    def _east_of_utc(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Berlin, where the developer reads the page and year 9999 overflows."""
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        yield
        time.tzset()  # monkeypatch has put TZ back; make the C library agree

    def test_a_date_a_table_cannot_print_is_an_empty_cell(self) -> None:
        """Empty is a state ``span_label`` already treats as legitimate."""
        assert short_date(datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)) == ""

    def test_the_other_end_of_the_range_is_empty_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Year 0001 overflows the other way, and only west of UTC.

        Which end blows up depends on the sign of the offset — adding one to
        year 9999 passes ``datetime.max``, subtracting one from year 0001 passes
        ``datetime.min`` — so a guard tested in one zone only is a guard tested
        in one direction only.
        """
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()

        assert short_date(datetime(1, 1, 1, tzinfo=UTC)) == ""

    def test_an_ordinary_date_still_prints(self) -> None:
        """The guard must not swallow the dates the page exists to show."""
        assert short_date(MARCH) == f"{MARCH.astimezone():%d.%m.%y}"

    async def test_one_unprintable_date_does_not_freeze_every_panel(
        self, state, graph
    ) -> None:
        """The whole failure, end to end: five spinners and no alert.

        The projection sat outside the failure boundary, so this raised out of
        ``load()`` and left every ``loading_*`` flag True with every error
        string empty — and ``guidance`` returns "" while the totals are still
        loading, so the page rendered five spinners, forever, with nothing to
        say and nothing to retry.
        """
        graph.rows(
            catalog.RECURRING_GROUPS,
            ["id", "size", "message_count", "first_seen", "last_seen"],
            [["a" * 64, 4, 9, MARCH.isoformat(), "9999-12-31T23:59:59+00:00"]],
        )

        await _load(state)

        assert state.busy is False
        assert state.groups_error == ""
        assert [one.span for one in state.groups] == [f"{MARCH.astimezone():%d.%m.%y}"]


class TestAPanelThatCannotBeProjected:
    """The boundary itself, not the one bug that happened to cross it.

    ``_answered`` exists so that "one dead panel must not take the other four
    with it", and the projections used to sit outside it — so anything raising
    between the row and the view killed the whole readout. Making
    :func:`short_date` total closes today's way in; this pins the structure, so
    the next projection that learns to raise costs one panel.
    """

    async def test_a_projection_that_raises_is_that_panels_error(
        self, state, graph, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(row: object) -> GroupView:
            raise ValueError("this row cannot be projected")

        monkeypatch.setattr(GroupView, "from_row", staticmethod(boom))

        await _load(state)

        assert state.groups_error == "this row cannot be projected"
        assert state.groups == []
        assert state.busy is False
        assert len(state.topics) == 2
        assert state.agreement.agrees is True

    async def test_the_cross_check_button_always_stops_its_own_spinner(
        self, state, graph, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``check_agreement`` had no ``finally``, so a raise left it spinning."""

        async def boom(
            _self: object, _reader: object
        ) -> tuple[object, list[object], str]:
            raise ValueError("nothing survives this")

        monkeypatch.setattr(AnalyticsInsightsState, "_read_agreement", boom)

        with pytest.raises(ValueError, match="nothing survives"):
            await _check_agreement(state)

        assert state.loading_agreement is False


PANELS = (
    "loading_totals",
    "loading_agreement",
    "loading_groups",
    "loading_topics",
    "loading_templates",
    "loading_communities",
    "loading_important",
    "loading_tags",
)
"""Every panel that answers on its own, and therefore every term of ``busy``.

Named once so the two tests below cannot drift apart from the state: a ninth
panel added without a flag here is a page-level spinner that stops early.
"""


class TestTheBusyFlag:
    """Eight panels, one page-level spinner, and never a mixed state in a test.

    Every assertion on ``busy`` was taken either before any read (all flags
    true) or after ``_apply`` (all false), so collapsing the ``or`` to a single
    term survived the whole suite. Written per panel instead: each one is set
    alone and has to hold the flag up on its own, which is the future the var
    exists for.
    """

    @pytest.mark.parametrize("panel", PANELS)
    def test_one_panel_still_waiting_is_still_busy(self, state, panel) -> None:
        for one in PANELS:
            setattr(state, one, one == panel)

        assert state.busy is True, f"{panel} alone no longer holds the spinner"

    def test_every_panel_answered_is_not_busy(self, state) -> None:
        for one in PANELS:
            setattr(state, one, False)

        assert state.busy is False


class TestTheProjection:
    def test_a_key_is_shortened_to_the_digest_it_carries(self) -> None:
        assert short_key("a" * 64) == "a" * 12 + "…"
        assert short_key("topic:" + "b" * 32) == "b" * 12 + "…"
        assert short_key("template:1a2b3c4d5e6f7a8b:sent") == "1a2b3c4d5e6f…"
        assert short_key("short") == "short"
        assert short_key("") == ""

    def test_a_span_is_one_date_when_both_ends_fall_on_one_day(self) -> None:
        assert span_label(MARCH, MARCH) == f"{MARCH.astimezone():%d.%m.%y}"
        assert span_label(None, None) == ""
        assert span_label(MARCH, None) == f"{MARCH.astimezone():%d.%m.%y}"
        assert " – " in span_label(MARCH, AUGUST)

    def test_a_sample_is_one_line_and_cut_where_a_row_stops_caring(self) -> None:
        assert sample_label("a\n\nb  c") == "a b c"
        assert sample_label("x" * 30, limit=10) == "x" * 10 + "…"

    def test_a_signal_this_build_does_not_know_is_grey_not_hidden(self) -> None:
        assert method_color("ref") == "teal"
        assert method_color("semantic") == "gray"
        assert method_color("") == "gray"

    def test_every_signal_the_analysis_can_write_has_a_colour(self) -> None:
        """The exhaustiveness this file was missing.

        `TopicSignal.CONVERSATION` was added to the analysis and not to
        `METHOD_COLORS`, and the miss reached a user: the first rebuild that
        wrote a `conversation` edge made the whole topics card raise
        `KeyError` instead of rendering. A signal is added in one package and
        coloured in another, so nothing but this assertion joins them.
        """
        assert set(METHOD_COLORS) == set(TopicSignal)

    def test_a_signal_with_no_colour_is_still_grey_rather_than_a_crash(self) -> None:
        """`method_color`'s own docstring promises grey "for one we do not
        know", but it caught only `ValueError` — the unknown-*string* case. A
        `TopicSignal` this build knows and has not coloured raised `KeyError`
        straight through it. Both misses are the same promise."""
        missing = next(iter(TopicSignal))

        with mock.patch.dict(METHOD_COLORS, clear=True):
            assert method_color(missing.value) == "gray"

    def test_a_view_cannot_be_edited_once_read(self) -> None:
        row = PairView(left_id="a", right_id="b", together=1)

        with pytest.raises(ValidationError):
            row.together = 99  # ty: ignore[invalid-assignment]

    async def test_what_reaches_the_browser_is_the_view_and_nothing_else(
        self, state
    ) -> None:
        """§9.1: a small projection, never the catalogue row with its
        datetimes and its float."""
        await _load(state)

        assert state.sent_templates[0].model_dump() == {
            "key": "1a2b3c4d5e6f…",
            "occurrences": 7,
            "score": 72.0,
            "score_label": "0.72",
            "sample": "Sehr geehrte Damen und Herren, anbei die Rechnung.",
            "span": (f"{MARCH.astimezone():%d.%m.%y} – {AUGUST.astimezone():%d.%m.%y}"),
        }


class TestThePollIsTurnedOffWhenThePageGoesAway:
    """``stop_polling`` existed, was documented as wired, and was not."""

    def test_the_panel_unmounts_into_stop_polling(self) -> None:
        """Asserted off the event triggers, not the rendered tree.

        ``render()`` puts props in and leaves handlers out, so a rendered
        string would have gone on passing with the wiring deleted — which is
        the shape that let the handler sit uncalled in the first place.
        """
        triggers = insights_panel().event_triggers

        assert "on_unmount" in triggers
        assert "stop_polling" in str(triggers["on_unmount"])


class TestTheComponents:
    """A prop appkit_mantine does not have only shows up when it is built."""

    @pytest.mark.parametrize(
        "build",
        [
            rebuild_controls,
            rebuild_card,
            totals_card,
            agreement_card,
            disputes_table,
            pairs_table,
            groups_card,
            topics_card,
            templates_card,
            communities_card,
            important_card,
            tags_card,
            guidance_panel,
            analyses,
            insights_panel,
        ],
    )
    def test_it_builds_and_renders(self, build) -> None:
        assert isinstance(build(), rx.Component)
        assert build().render()

    @pytest.mark.parametrize(
        ("build", "binding"),
        [
            # SS6.2's fact-versus-suggestion column: a hard-coded colour makes
            # every topic look equally trustworthy.
            (topics_card, 'row_rx_state_?.["method_color"]'),
            # How long a pair has been alive; the column was deletable.
            (pairs_table, 'row_rx_state_?.["span"]'),
            # What the verdict did NOT cover — agreement is worth exactly as
            # much as the share of the archive it was drawn over.
            (agreement_card, 'agreement_rx_state_?.["unjudged"]'),
            (agreement_card, 'agreement_rx_state_?.["coverage"]'),
            # "Showing the heaviest N of M, and a dash means…": built and
            # asserted in the model, never asserted as rendered.
            (disputes_table, 'agreement_rx_state_?.["disputes_note"]'),
            # Every panel's alert, asserted on the *condition* and not merely
            # on the presence of the var: `rx.cond` compiles to a ternary that
            # carries both branches whatever the test says, so `error != ""`
            # replaced by `False` leaves every binding in the rendered string
            # and only the comparison disappears. `?.valueOf?.()` is what a
            # Var comparison renders as.
            (groups_card, "groups_error_rx_state_?.valueOf?.()"),
            (topics_card, "topics_error_rx_state_?.valueOf?.()"),
            (templates_card, "templates_error_rx_state_?.valueOf?.()"),
            (communities_card, "communities_error_rx_state_?.valueOf?.()"),
            (important_card, "important_error_rx_state_?.valueOf?.()"),
            (tags_card, "tag_error_rx_state_?.valueOf?.()"),
            # What a topic is about, in its members' own words — a column that
            # renders nothing at all if the keywords never reach the row.
            (topics_card, 'row_rx_state_?.["keywords"]'),
            # The circle's two numbers: forty people who exchanged three mails
            # and five who exchanged four hundred are not the same finding.
            (communities_card, 'row_rx_state_?.["size"]'),
            (communities_card, 'row_rx_state_?.["message_count"]'),
            # B2's whole point: a score a reader cannot argue with is not what
            # SS1.1 asked for, so the reasons are as load-bearing as the bar.
            (important_card, 'row_rx_state_?.["score"]'),
            (important_card, 'row_rx_state_?.["reasons"]'),
            # R8's badge, and the count that makes the notice under it true.
            (tags_card, 'tag_rx_state_?.["suggestions"]'),
        ],
    )
    def test_the_binding_a_reader_depends_on_is_actually_rendered(
        self, build, binding
    ) -> None:
        """The parametrized build-and-render test above cannot fail on content.

        ``isinstance(…, rx.Component)`` and a truthy ``render()`` hold for any
        component, so five separate deletions survived the whole suite at 100%
        line and branch coverage: the topic badge's colour, every panel's error
        alert, the unjudged count, the disputes caption and the pairs table's
        span column. A rendered Reflex tree can be asked one thing cheaply —
        which state var a node reads — and that is what turns a smoke test into
        a contract.
        """
        assert binding in str(build().render())

    def test_the_dispute_rows_read_their_fields_off_the_view(self) -> None:
        """Reflex resolves ``row.field`` inside a foreach over a pydantic
        model — the reason none of these projections is a dataclass."""
        rendered = str(disputes_table().render())

        assert 'row_rx_state_?.["note_color"]' in rendered
        assert 'row_rx_state_?.["truth"]' in rendered
        assert 'row_rx_state_?.["edge"]' in rendered

    @pytest.mark.parametrize(
        "build",
        [
            disputes_table,
            pairs_table,
            groups_card,
            topics_card,
            templates_card,
            communities_card,
            important_card,
        ],
    )
    def test_every_listing_is_a_window_of_rows(self, build) -> None:
        """Twelve rows under a header that stays put, on all five.

        Asserted per listing rather than once over the panel: the page is
        five separate tables and a sixth added next to them would be the one
        that scrolls the card off the screen again.
        """
        rendered = str(build().render())

        assert "ma-table-scroll" in rendered, "the box that scrolls is missing"
        assert "stickyHeader" in rendered, "the column names would scroll away"

    def test_the_page_no_longer_carries_a_search_box(self) -> None:
        """Finding a message is ``/``'s job, and only ``/``'s.

        Two boxes over one archive is how the two came to word the same
        answer differently; the one here searched without filters, without an
        account picker and with nowhere to read the result.
        """
        rendered = str(insights_panel().render())

        assert "Find a message" not in rendered
        assert "Search the archive" not in rendered

    def test_the_verdict_takes_its_colour_from_the_state(self) -> None:
        """Red or yellow is the whole message of the panel; a hard-coded
        colour would make the check decorative."""
        rendered = str(agreement_card().render())

        assert 'agreement_rx_state_?.["color"]' in rendered
        assert 'agreement_rx_state_?.["headline"]' in rendered
