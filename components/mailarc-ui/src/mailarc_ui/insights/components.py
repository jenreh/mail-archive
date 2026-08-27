"""The insights panels: what a rebuild wrote, and whether A1 can be believed.

Layout and nothing else. Every value comes from a state —
:class:`AnalyticsInsightsState` for the analyses,
:class:`~mailarc_ui.insights.search.ArchiveSearchState` for the search box;
nothing here opens a graph or runs a statement. The order down the page is the
order a person checks an analysis in — what is in the archive, what a rebuild
made of it, then whether the one derived thing that can be verified still
verifies, then the three listings — so the sceptical question comes before the
findings that depend on the answer. The search card is the exception and sits
near the top: it is the one thing here that answers over a fresh archive
nobody has derived anything from.

The cross-check panel is deliberately the loudest thing on the page. It is the
only panel that can say something is *wrong* rather than merely say what was
found, and the verdict's colour is the whole of that message: red only when the
edge claims more than the archive supports, because that is the one direction
nothing legitimate produces.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.insights.model import (
    DisputeView,
    GroupView,
    HitView,
    PairView,
    TemplateView,
    TopicView,
)
from mailarc_ui.insights.search import SEARCH_PATHS, ArchiveSearchState
from mailarc_ui.insights.state import AnalyticsInsightsState
from mailarc_ui.kit import card_heading, panel_card, stat_tile

KEY_COLUMN = {"width": 140}
"""A digest column: wide enough for twelve characters, and no wider."""

ADDRESS_COLUMN = {"maxWidth": 260, "overflow": "hidden", "textOverflow": "ellipsis"}
"""An address column. Mail addresses are long and a table is not a place to
read one in full — the numbers beside it are what the row is for."""


def totals_card() -> rx.Component:
    """The seven counts, ground truth and derived layer side by side.

    Together rather than in two cards, because the only useful reading is the
    ratio: two hundred templates over thirty messages is a calibration failure
    and either number on its own looks fine. ``Unidentified`` turns red when it
    is not zero — a ``Message`` without a canonical id is something the graph
    holds that the writer cannot produce, and every analysis steps over it.
    """
    return panel_card(
        mn.stack(
            card_heading("database", "The archive, and what was derived"),
            rx.cond(
                AnalyticsInsightsState.loading_totals,
                mn.group(mn.loader(size="sm"), justify="center", py="lg"),
                mn.simple_grid(
                    stat_tile("Messages", AnalyticsInsightsState.totals.messages),
                    stat_tile(
                        "Unidentified",
                        AnalyticsInsightsState.totals.unidentified,
                        color=rx.cond(
                            AnalyticsInsightsState.totals.unidentified > 0,
                            "red",
                            "inherit",
                        ),
                    ),
                    stat_tile("Pairs", AnalyticsInsightsState.totals.co_addressed),
                    stat_tile("Groups", AnalyticsInsightsState.totals.groups),
                    stat_tile("Topics", AnalyticsInsightsState.totals.topics),
                    stat_tile("Templates", AnalyticsInsightsState.totals.templates),
                    stat_tile("Derived", AnalyticsInsightsState.totals.derived),
                    cols={"base": 2, "sm": 4, "lg": 7},
                    spacing="md",
                ),
            ),
            gap="sm",
        ),
    )


def rebuild_controls() -> rx.Component:
    """Rebuild, cancel, re-check, refresh — everything that starts work."""
    return mn.group(
        mn.button(
            "Rebuild",
            on_click=AnalyticsInsightsState.start_rebuild,
            loading=AnalyticsInsightsState.starting,
            disabled=~AnalyticsInsightsState.can_rebuild,
            left_section=rx.icon("play", size=14),
            variant="filled",
            size="xs",
        ),
        mn.button(
            "Cancel",
            on_click=AnalyticsInsightsState.cancel_rebuild,
            loading=AnalyticsInsightsState.cancelling,
            disabled=~AnalyticsInsightsState.can_cancel,
            left_section=rx.icon("square", size=14),
            variant="light",
            color="red",
            size="xs",
        ),
        mn.button(
            "Refresh",
            on_click=AnalyticsInsightsState.load,
            loading=AnalyticsInsightsState.busy,
            left_section=rx.icon("refresh-cw", size=14),
            variant="light",
            size="xs",
        ),
        gap="sm",
    )


def rebuild_card() -> rx.Component:
    """Start a rebuild and watch it, without leaving the page it changes.

    The bar climbs in stages and not in messages: the worker moves the row on
    once per analysis, so ``3 of 5 stages`` is what the row honestly knows. A
    percentage of the archive would be a nicer number and a made-up one.
    """
    return panel_card(
        mn.stack(
            mn.group(
                card_heading("hammer", "Rebuild the derived layer"),
                rebuild_controls(),
                justify="space-between",
                align="center",
                w="100%",
            ),
            mn.text(
                "Deletes everything derived and computes it again from the "
                "archive. Running it twice over an unchanged archive changes "
                "nothing.",
                size="xs",
                c="dimmed",
            ),
            rx.cond(
                AnalyticsInsightsState.has_job,
                mn.group(
                    mn.badge(
                        AnalyticsInsightsState.job.status,
                        color=AnalyticsInsightsState.job.status_color,
                        variant="light",
                        size="sm",
                    ),
                    mn.progress(
                        value=AnalyticsInsightsState.job.percent,
                        color="blue",
                        size="lg",
                        striped=AnalyticsInsightsState.job.active,
                        animated=AnalyticsInsightsState.job.active,
                        flex="1",
                    ),
                    mn.text(
                        AnalyticsInsightsState.job.percent_label,
                        size="sm",
                        fw=600,
                        w=52,
                        ta="right",
                    ),
                    mn.text(
                        AnalyticsInsightsState.job.stages_label,
                        size="sm",
                        c="dimmed",
                    ),
                    gap="sm",
                    align="center",
                    w="100%",
                ),
                mn.text(""),
            ),
            rx.cond(
                AnalyticsInsightsState.rebuild_message != "",
                mn.alert(
                    AnalyticsInsightsState.rebuild_message,
                    color="yellow",
                    variant="light",
                    py="xs",
                ),
                mn.text(""),
            ),
            rx.cond(
                AnalyticsInsightsState.job.error != "",
                mn.alert(
                    AnalyticsInsightsState.job.error,
                    title="The rebuild stopped with an error",
                    color="red",
                    variant="light",
                    icon=rx.icon("triangle-alert", size=16),
                ),
                mn.text(""),
            ),
            gap="sm",
        ),
    )


def _verdict() -> rx.Component:
    """The headline, the sentence under it, and how much it covered."""
    return mn.alert(
        mn.stack(
            mn.text(AnalyticsInsightsState.agreement.headline, fw=700, size="sm"),
            mn.text(AnalyticsInsightsState.agreement.detail, size="xs"),
            mn.text(AnalyticsInsightsState.agreement.coverage, size="xs", c="dimmed"),
            gap=4,
        ),
        color=AnalyticsInsightsState.agreement.color,
        variant="light",
        icon=rx.icon("scale", size=16),
    )


def _agreement_counts() -> rx.Component:
    """The four buckets and what was left open, as numbers."""
    return mn.simple_grid(
        stat_tile("Matched", AnalyticsInsightsState.agreement.matched),
        stat_tile("Different counts", AnalyticsInsightsState.agreement.mismatched),
        stat_tile("Edge only", AnalyticsInsightsState.agreement.edge_only),
        stat_tile("Archive only", AnalyticsInsightsState.agreement.truth_only),
        stat_tile("Unjudged", AnalyticsInsightsState.agreement.unjudged),
        cols={"base": 2, "sm": 5},
        spacing="md",
    )


def _dispute_row(row: DisputeView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.text(row.left_id, size="sm"), style=ADDRESS_COLUMN),
        mn.table.td(mn.text(row.right_id, size="sm"), style=ADDRESS_COLUMN),
        mn.table.td(row.truth),
        mn.table.td(row.edge),
        mn.table.td(
            mn.badge(
                row.note, color=row.note_color, variant="light", size="sm", tt="none"
            )
        ),
    )


def disputes_table() -> rx.Component:
    """Every pair the two readings of A1 disagree about, loudest first."""
    return mn.stack(
        mn.table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Address"),
                    mn.table.th("Address"),
                    mn.table.th("Archive"),
                    mn.table.th("Edge"),
                    mn.table.th("What it means"),
                ),
            ),
            mn.table.tbody(
                rx.foreach(AnalyticsInsightsState.agreement.disputes, _dispute_row)
            ),
            striped=True,
            highlight_on_hover=True,
            tabular_nums=True,
        ),
        mn.text(AnalyticsInsightsState.agreement.disputes_note, size="xs", c="dimmed"),
        gap="xs",
    )


def _pair_row(row: PairView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.text(row.left_id, size="sm"), style=ADDRESS_COLUMN),
        mn.table.td(mn.text(row.right_id, size="sm"), style=ADDRESS_COLUMN),
        mn.table.td(row.together),
        mn.table.td(row.span),
    )


def pairs_table() -> rx.Component:
    """A1 itself: who keeps being written to together, heaviest pair first."""
    return mn.table(
        mn.table.thead(
            mn.table.tr(
                mn.table.th("Address"),
                mn.table.th("Address"),
                mn.table.th("Together"),
                mn.table.th("Seen"),
            ),
        ),
        mn.table.tbody(rx.foreach(AnalyticsInsightsState.pairs, _pair_row)),
        striped=True,
        highlight_on_hover=True,
        tabular_nums=True,
    )


def agreement_card() -> rx.Component:
    """The cross-check: A1's definition against A1's materialised edge.

    The one panel that can fail rather than merely report. Both numbers stay on
    screen next to the verdict — the ranking underneath is what the edge says,
    and a reader who has just been told the edge is trustworthy should be able
    to look at what it claims in the same breath.
    """
    return panel_card(
        mn.stack(
            mn.group(
                card_heading("scale", "Co-addressed pairs, checked twice"),
                mn.button(
                    "Re-check",
                    on_click=AnalyticsInsightsState.check_agreement,
                    loading=AnalyticsInsightsState.loading_agreement,
                    left_section=rx.icon("refresh-cw", size=14),
                    variant="light",
                    size="xs",
                ),
                justify="space-between",
                align="center",
                w="100%",
            ),
            mn.text(
                "The same question asked twice: once by walking SENT_TO and "
                "COPIED_TO, once by reading the CO_ADDRESSED edge a rebuild "
                "wrote. If the two disagree, the edge is wrong.",
                size="xs",
                c="dimmed",
            ),
            rx.cond(
                AnalyticsInsightsState.agreement_error != "",
                _panel_error(AnalyticsInsightsState.agreement_error),
                rx.cond(
                    AnalyticsInsightsState.loading_agreement,
                    mn.group(mn.loader(size="sm"), justify="center", py="lg"),
                    mn.stack(
                        _verdict(),
                        _agreement_counts(),
                        rx.cond(
                            AnalyticsInsightsState.agreement.disputes,
                            disputes_table(),
                            mn.text(""),
                        ),
                        mn.divider(label="What the edge says", label_position="left"),
                        rx.cond(
                            AnalyticsInsightsState.pairs,
                            pairs_table(),
                            mn.text("No pairs are stored.", size="sm", c="dimmed"),
                        ),
                        gap="md",
                    ),
                ),
            ),
            gap="sm",
        ),
    )


def _group_row(row: GroupView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.code(row.key), style=KEY_COLUMN),
        mn.table.td(row.size),
        mn.table.td(row.message_count),
        mn.table.td(row.span),
    )


def groups_card() -> rx.Component:
    """A1's other half: the circles of people that keep being written to."""
    return _panel(
        icon="users",
        title="Recurring groups",
        hint=(
            "One row per participant key — the sender and every recipient of a "
            "message, hashed. Not a clique search: two messages to the same set "
            "of people share a key however the client reordered them."
        ),
        error=AnalyticsInsightsState.groups_error,
        loading=AnalyticsInsightsState.loading_groups,
        anything=AnalyticsInsightsState.groups,
        nothing="No group met the size and message thresholds.",
        body=mn.table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Key"),
                    mn.table.th("People"),
                    mn.table.th("Messages"),
                    mn.table.th("Seen"),
                ),
            ),
            mn.table.tbody(rx.foreach(AnalyticsInsightsState.groups, _group_row)),
            striped=True,
            highlight_on_hover=True,
            tabular_nums=True,
        ),
    )


def _topic_row(row: TopicView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.text(row.label, size="sm", line_clamp=2)),
        mn.table.td(
            mn.badge(
                row.method,
                color=row.method_color,
                variant="light",
                size="sm",
                tt="none",
            )
        ),
        mn.table.td(row.messages),
        mn.table.td(mn.code(row.key), style=KEY_COLUMN),
    )


def topics_card() -> rx.Component:
    """A2, with the signal that drew each cluster's edges as a colour.

    The method column is not decoration. §6.2 makes it the difference between a
    fact and a suggestion — ``ref`` is two messages naming the same ticket,
    ``participants`` is two messages merely sent to the same people — and a
    topic appears once per method precisely so the two never get added up.
    """
    return _panel(
        icon="tags",
        title="Topics",
        hint=(
            "One row per topic and signal, biggest first. Cool badges are the "
            "signals that carry a topic on their own; warm ones only carry one "
            "together with another."
        ),
        error=AnalyticsInsightsState.topics_error,
        loading=AnalyticsInsightsState.loading_topics,
        anything=AnalyticsInsightsState.topics,
        nothing="Nothing clustered above the topic score threshold.",
        body=mn.table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Topic"),
                    mn.table.th("Signal"),
                    mn.table.th("Messages"),
                    mn.table.th("Key"),
                ),
            ),
            mn.table.tbody(rx.foreach(AnalyticsInsightsState.topics, _topic_row)),
            striped=True,
            highlight_on_hover=True,
            tabular_nums=True,
        ),
    )


def _template_row(row: TemplateView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(
            mn.group(
                mn.progress(value=row.score, color="grape", size="sm", w=64),
                mn.text(row.score_label, size="sm", fw=600),
                gap="xs",
                wrap="nowrap",
                align="center",
            ),
        ),
        mn.table.td(row.occurrences),
        mn.table.td(mn.text(row.sample, size="sm", line_clamp=2)),
        mn.table.td(row.span),
    )


def _template_table(rows: rx.Var | list[TemplateView]) -> rx.Component:
    return mn.table(
        mn.table.thead(
            mn.table.tr(
                mn.table.th("Automatable"),
                mn.table.th("Times"),
                mn.table.th("Sample"),
                mn.table.th("Seen"),
            ),
        ),
        mn.table.tbody(rx.foreach(rows, _template_row)),
        striped=True,
        highlight_on_hover=True,
        tabular_nums=True,
    )


def templates_card() -> rx.Component:
    """A3, sent and received kept visibly apart.

    §6.3's rule, and the reason the two are never one ranking: only what you
    write yourself is automatable, and the score is calibrated within a
    direction and means nothing across one. A receipt you get every month and a
    reply you type every month score alike and are not the same finding.
    """
    return _panel(
        icon="repeat",
        title="Templates",
        hint=(
            "Texts that keep being written, best candidate first. The score is "
            "frequency times regularity times brevity, and only comparable "
            "within one direction."
        ),
        error=AnalyticsInsightsState.templates_error,
        loading=AnalyticsInsightsState.loading_templates,
        anything=AnalyticsInsightsState.has_templates,
        nothing="No text repeated closely enough to be a template.",
        body=mn.stack(
            mn.divider(label="Written by this archive", label_position="left"),
            rx.cond(
                AnalyticsInsightsState.sent_templates,
                _template_table(AnalyticsInsightsState.sent_templates),
                mn.text("Nothing sent repeats.", size="sm", c="dimmed"),
            ),
            mn.divider(label="Received", label_position="left"),
            rx.cond(
                AnalyticsInsightsState.received_templates,
                _template_table(AnalyticsInsightsState.received_templates),
                mn.text("Nothing received repeats.", size="sm", c="dimmed"),
            ),
            gap="sm",
        ),
    )


def _search_controls() -> rx.Component:
    """The box and the button. Enter does what the button does."""
    return mn.group(
        mn.text_input(
            placeholder="rechnung swiftscan",
            default_value="",
            on_change=ArchiveSearchState.set_query,
            on_key_down=ArchiveSearchState.search_on_enter,
            left_section=rx.icon("search", size=14),
            left_section_pointer_events="none",
            aria_label="Search the archive",
            size="sm",
            flex="1",
        ),
        mn.button(
            "Search",
            on_click=ArchiveSearchState.run,
            loading=ArchiveSearchState.searching,
            disabled=~ArchiveSearchState.can_search,
            left_section=rx.icon("search", size=14),
            variant="filled",
            size="sm",
        ),
        gap="sm",
        align="center",
        wrap="nowrap",
        w="100%",
    )


def _search_path() -> rx.Component:
    """Which of the two searches runs, and which model the other one uses."""
    return mn.group(
        rx.cond(
            ArchiveSearchState.embedding_model != "",
            mn.badge(
                ArchiveSearchState.embedding_model,
                color="grape",
                variant="light",
                size="sm",
                tt="none",
            ),
            mn.text(""),
        ),
        mn.segmented_control(
            data=SEARCH_PATHS,
            value=ArchiveSearchState.kind,
            on_change=ArchiveSearchState.choose_path,
            size="xs",
        ),
        gap="xs",
        align="center",
        wrap="nowrap",
    )


def _hit_row(row: HitView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(
            mn.group(
                mn.progress(value=row.score, color="blue", size="sm", w=64),
                mn.text(row.score_label, size="sm", fw=600),
                gap="xs",
                wrap="nowrap",
                align="center",
            ),
        ),
        mn.table.td(
            mn.stack(
                mn.text(row.subject, size="sm", line_clamp=2),
                mn.text(row.message_id, size="xs", c="dimmed", line_clamp=1),
                gap=0,
            ),
        ),
        mn.table.td(mn.text(row.sender, size="sm"), style=ADDRESS_COLUMN),
        mn.table.td(row.when),
    )


def hits_table() -> rx.Component:
    """What the search found, in the order the index ranked it."""
    return mn.table(
        mn.table.thead(
            mn.table.tr(
                mn.table.th("Match"),
                mn.table.th("Subject"),
                mn.table.th("From"),
                mn.table.th("Sent"),
            ),
        ),
        mn.table.tbody(rx.foreach(ArchiveSearchState.hits, _hit_row)),
        striped=True,
        highlight_on_hover=True,
        tabular_nums=True,
    )


def search_card() -> rx.Component:
    """Find a message: the words it holds, or what it is about.

    The panel a fresh installation meets first, and the one place on this page
    where "nothing configured" must not look like "nothing found". With no
    embedder the semantic half says so in the sentence that names the setting
    to change, its button is dead, and the full-text half goes on working —
    which is §7.4's whole point: an archive with no model configured is a
    complete archive missing one way of asking.

    Its own ``on_mount`` rather than a place in the page's ``on_load`` chain:
    what it primes is a different service from the readout's, and neither
    should be able to leave the other unasked.
    """
    return panel_card(
        mn.stack(
            mn.group(
                card_heading("search", "Find a message"),
                _search_path(),
                justify="space-between",
                align="center",
                w="100%",
            ),
            mn.text(
                "Full text finds the words you type, in the subject or the "
                "body. Semantic finds messages about the same thing, "
                "including ones that never use the word — it needs an "
                "embedder and a finished embed job. The two scores are not "
                "comparable, so a search runs one path at a time.",
                size="xs",
                c="dimmed",
            ),
            _search_controls(),
            rx.cond(
                ArchiveSearchState.semantic_blocked,
                mn.alert(
                    ArchiveSearchState.semantic_note,
                    color="yellow",
                    variant="light",
                    py="xs",
                    icon=rx.icon("triangle-alert", size=16),
                ),
                mn.text(""),
            ),
            rx.cond(
                ArchiveSearchState.error != "",
                mn.alert(
                    ArchiveSearchState.error,
                    color=ArchiveSearchState.error_color,
                    variant="light",
                    py="xs",
                    icon=rx.icon("triangle-alert", size=16),
                ),
                rx.cond(
                    ArchiveSearchState.searching,
                    mn.group(mn.loader(size="sm"), justify="center", py="lg"),
                    mn.stack(
                        rx.cond(
                            ArchiveSearchState.summary != "",
                            mn.text(ArchiveSearchState.summary, size="xs", c="dimmed"),
                            mn.text(""),
                        ),
                        rx.cond(ArchiveSearchState.hits, hits_table(), mn.text("")),
                        rx.cond(
                            ArchiveSearchState.notice != "",
                            mn.alert(
                                ArchiveSearchState.notice,
                                color="yellow",
                                variant="light",
                                py="xs",
                                icon=rx.icon("info", size=16),
                            ),
                            mn.text(""),
                        ),
                        gap="xs",
                        w="100%",
                    ),
                ),
            ),
            gap="sm",
        ),
        on_mount=ArchiveSearchState.prepare,
    )


def _panel_error(message: rx.Var | str) -> rx.Component:
    """What a panel shows instead of its table when the graph did not answer.

    Instead of, and never above: half a table under a red alert reads as data,
    and the numbers in it would be from whenever the last read succeeded.
    """
    return mn.alert(
        message,
        color="red",
        variant="light",
        py="xs",
        icon=rx.icon("triangle-alert", size=16),
    )


def _panel(
    *,
    icon: str,
    title: str,
    hint: str,
    error: rx.Var | str,
    loading: rx.Var | bool,
    anything: rx.Var | list | bool,
    nothing: str,
    body: rx.Component,
) -> rx.Component:
    """One analysis in one card: what it is, and what it answered.

    The three listings differ only in their table, so everything around one is
    written once — the heading, the sentence saying what the numbers mean, the
    spinner while the graph is being asked, the alert when it did not answer,
    and the line when it answered with nothing. ``anything`` is whatever reads
    as false when the panel has nothing to show: a list var already does, so
    most panels pass their rows straight in.
    """
    return panel_card(
        mn.stack(
            card_heading(icon, title),
            mn.text(hint, size="xs", c="dimmed"),
            rx.cond(
                error != "",
                _panel_error(error),
                rx.cond(
                    loading,
                    mn.group(mn.loader(size="sm"), justify="center", py="lg"),
                    rx.cond(anything, body, mn.text(nothing, size="sm", c="dimmed")),
                ),
            ),
            gap="sm",
        ),
    )


def guidance_panel() -> rx.Component:
    """What the page says when there is nothing worth tabulating yet.

    A fresh archive is the first thing anybody sees here, and four empty tables
    would let them conclude the analyses found nothing — which is a different
    statement from "nothing has been run". The button is in the panel because
    the answer to the sentence above it is one click away.
    """
    return panel_card(
        mn.empty_state(
            rx.cond(
                AnalyticsInsightsState.needs_rebuild,
                mn.empty_state.actions(
                    mn.button(
                        "Rebuild now",
                        on_click=AnalyticsInsightsState.start_rebuild,
                        loading=AnalyticsInsightsState.starting,
                        disabled=~AnalyticsInsightsState.can_rebuild,
                        left_section=rx.icon("play", size=14),
                        variant="filled",
                        size="sm",
                    ),
                ),
                mn.text(""),
            ),
            icon=rx.icon("sparkles", size=28),
            title="Nothing to analyse yet",
            description=AnalyticsInsightsState.guidance,
            align="center",
        ),
    )


def analyses() -> rx.Component:
    """The four listings, sceptical panel first."""
    return mn.stack(
        agreement_card(),
        groups_card(),
        topics_card(),
        templates_card(),
        gap="md",
        w="100%",
    )


def insights_panel() -> rx.Component:
    """The whole page's body, for a page to drop in.

    Grows with its content rather than scrolling inside itself: these are
    tables of at most a screenful each, and a panel that owned its own height
    would collapse to nothing the moment a parent did not have a definite one.

    Owns the ``on_unmount`` because it owns the rebuild card that starts the
    poll. Without it a user who navigates away mid-rebuild leaves a background
    task asking the database every two seconds for the rest of the session —
    one per abandoned page. ``stop_polling`` was written for this and had no
    caller anywhere in the repository.

    The search card sits above the totals and outside the branch that hides
    everything when they fail, because it depends on none of it: a different
    service answers it, full text needs no rebuild, and a graph that could not
    be counted is a state the panel reports for itself rather than one that
    should take the search box off the page.
    """
    return mn.stack(
        rebuild_card(),
        search_card(),
        rx.cond(
            AnalyticsInsightsState.totals_error != "",
            _panel_error(AnalyticsInsightsState.totals_error),
            mn.stack(
                totals_card(),
                rx.cond(
                    AnalyticsInsightsState.guidance != "",
                    guidance_panel(),
                    analyses(),
                ),
                gap="md",
                w="100%",
            ),
        ),
        gap="md",
        w="100%",
        on_unmount=AnalyticsInsightsState.stop_polling,
    )
