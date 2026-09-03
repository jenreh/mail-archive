"""The insights panels: what a rebuild wrote, and whether A1 can be believed.

Layout and nothing else. Every value comes from
:class:`AnalyticsInsightsState`; nothing here opens a graph or runs a
statement. The order down the page is the order a person checks an analysis in
— what is in the archive, what a rebuild made of it, then whether the one
derived thing that can be verified still verifies, then the listings — so the
sceptical question comes before the findings that depend on the answer.

Finding a message is not one of them. This page had a search box of its own
while the archive still opened on a dashboard; the redesign made search the
front door (``/``), with filters, an account picker and a reading pane beside
the results, and a second smaller box here answered the same question worse.

The cross-check panel is deliberately the loudest thing on the page. It is the
only panel that can say something is *wrong* rather than merely say what was
found, and the verdict's colour is the whole of that message: red only when the
edge claims more than the archive supports, because that is the one direction
nothing legitimate produces.

Every listing is a :func:`~mailarc_ui.kit.scroll_table`: twelve rows with the
column names pinned above them, because these are rankings and a ranking is
read from the top. The tags card is the exception and not a ranking — the
population is what a person made by hand — so it is a stack of rows with the
two bulk verbs on each.

Every listing also carries a pill into ``/graph``, rooted at the row. Two of
the four roots go stale by design (R7) — a topic id and a circle id are digests
of their members, so a rebuild mints new ones — and the explorer answers a
cluster it can no longer find with a sentence rather than an empty canvas. A
message id and a tag id are the two that keep.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.insights.model import (
    CommunityView,
    DisputeView,
    GroupView,
    ImportantMessageView,
    PairView,
    TemplateView,
    TopicView,
)
from mailarc_ui.insights.state import AnalyticsInsightsState
from mailarc_ui.kit import (
    card_heading,
    empty_panel,
    job_progress,
    label_chip,
    message,
    panel_card,
    pill_action,
    primary_button,
    score_bar,
    scroll_table,
    soft_button,
    spinner,
    stat_tile,
    status_badge,
    toned_message,
)
from mailarc_ui.shell import routes
from mailarc_ui.tags import DEFAULT_TAG_COLOR, TagView

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
                spinner(),
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
        primary_button(
            "Rebuild",
            on_click=AnalyticsInsightsState.start_rebuild,
            loading=AnalyticsInsightsState.starting,
            disabled=~AnalyticsInsightsState.can_rebuild,
            left_section=rx.icon("play", size=14),
            size="xs",
        ),
        soft_button(
            "Cancel",
            on_click=AnalyticsInsightsState.cancel_rebuild,
            loading=AnalyticsInsightsState.cancelling,
            disabled=~AnalyticsInsightsState.can_cancel,
            left_section=rx.icon("square", size=14),
            color="red",
            size="xs",
        ),
        soft_button(
            "Refresh",
            on_click=AnalyticsInsightsState.load,
            loading=AnalyticsInsightsState.busy,
            left_section=rx.icon("refresh-cw", size=14),
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
                job_progress(
                    AnalyticsInsightsState.job.percent,
                    AnalyticsInsightsState.job.percent_label,
                    AnalyticsInsightsState.job.stages_label,
                    AnalyticsInsightsState.job.active,
                    status=AnalyticsInsightsState.job.status,
                    status_color=AnalyticsInsightsState.job.status_color,
                ),
                mn.text(""),
            ),
            rx.cond(
                AnalyticsInsightsState.rebuild_message != "",
                message(AnalyticsInsightsState.rebuild_message, "warning"),
                mn.text(""),
            ),
            rx.cond(
                AnalyticsInsightsState.job.error != "",
                message(
                    AnalyticsInsightsState.job.error,
                    "failure",
                    title="The rebuild stopped with an error",
                ),
                mn.text(""),
            ),
            gap="sm",
        ),
    )


def _verdict() -> rx.Component:
    """The headline, the sentence under it, and how much it covered."""
    return toned_message(
        mn.stack(
            mn.text(AnalyticsInsightsState.agreement.headline, fw=700, size="sm"),
            mn.text(AnalyticsInsightsState.agreement.detail, size="xs"),
            mn.text(AnalyticsInsightsState.agreement.coverage, size="xs", c="dimmed"),
            gap=4,
        ),
        AnalyticsInsightsState.agreement.color,
        icon="scale",
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
        mn.table.td(status_badge(row.note, row.note_color)),
    )


def disputes_table() -> rx.Component:
    """Every pair the two readings of A1 disagree about, loudest first."""
    return mn.stack(
        scroll_table(
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
    return scroll_table(
        mn.table.thead(
            mn.table.tr(
                mn.table.th("Address"),
                mn.table.th("Address"),
                mn.table.th("Together"),
                mn.table.th("Seen"),
            ),
        ),
        mn.table.tbody(rx.foreach(AnalyticsInsightsState.pairs, _pair_row)),
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
                soft_button(
                    "Re-check",
                    on_click=AnalyticsInsightsState.check_agreement,
                    loading=AnalyticsInsightsState.loading_agreement,
                    left_section=rx.icon("refresh-cw", size=14),
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
                    spinner(),
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
        body=scroll_table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Key"),
                    mn.table.th("People"),
                    mn.table.th("Messages"),
                    mn.table.th("Seen"),
                ),
            ),
            mn.table.tbody(rx.foreach(AnalyticsInsightsState.groups, _group_row)),
        ),
    )


def _graph_link(view: str) -> str:
    """The explorer, rooted at one row of a listing on this page.

    Concatenated rather than joined by a shared helper — see the same shape in
    ``search/components.py``. What is worth knowing is that two of the four
    links *can* go stale: a topic id and a circle id are digests of their
    members and every rebuild mints new ones (R7), so a page left open across a
    rebuild sends its reader to a cluster that no longer exists. The explorer
    answers that with a sentence rather than an empty canvas, which is the
    whole reason it is safe to offer the link at all. A message id and a tag id
    keep, and a tag is the durable reference a cluster is not.
    """
    return f"{routes.GRAPH}?view={view}&id="


TOPIC_LINK = _graph_link("topic")
COMMUNITY_LINK = _graph_link("community")
TAG_LINK = _graph_link("tag")
MESSAGE_LINK = _graph_link("message")
"""One per listing, because the four differ only in that one word — and a
communities pill built from the topic link would send a reader to a topic id
no ``Topic`` answers to, which the explorer reports as a recomputed cluster
rather than as a wrong link."""


def _graph_pill(link: str, node_id: str) -> rx.Component:
    """Take this row to the explorer, rooted at itself."""
    return pill_action(
        "Graph",
        icon="waypoints",
        on_click=rx.redirect(link + node_id),
    )


def _word_chips(
    words: rx.Var | list[str], color: rx.Var | str = "gray.6"
) -> rx.Component:
    """A row's short list of words, each in its own pill.

    Two listings print one: a topic's keywords and a message's importance
    reasons. Both are a handful of terms whose *number* is part of the reading
    — three reasons is a stronger claim than one — so they wrap rather than
    being joined into a sentence that a narrow column would cut in the middle.
    """
    return mn.group(
        rx.foreach(words, lambda word: label_chip(word, color)),
        gap=4,
        align="center",
        wrap="wrap",
    )


def _topic_row(row: TopicView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.text(row.label, size="sm", line_clamp=2)),
        mn.table.td(status_badge(row.method, row.method_color)),
        mn.table.td(_word_chips(row.keywords)),
        mn.table.td(row.messages),
        mn.table.td(mn.code(row.key), style=KEY_COLUMN),
        mn.table.td(_graph_pill(TOPIC_LINK, row.id)),
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
        body=scroll_table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Topic"),
                    mn.table.th("Signal"),
                    mn.table.th("About"),
                    mn.table.th("Messages"),
                    mn.table.th("Key"),
                    mn.table.th(""),
                ),
            ),
            mn.table.tbody(rx.foreach(AnalyticsInsightsState.topics, _topic_row)),
        ),
    )


def _community_row(row: CommunityView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.text(row.label, size="sm", line_clamp=1)),
        mn.table.td(row.size),
        mn.table.td(row.message_count),
        mn.table.td(row.span),
        mn.table.td(mn.code(row.key), style=KEY_COLUMN),
        mn.table.td(_graph_pill(COMMUNITY_LINK, row.id)),
    )


def communities_card() -> rx.Component:
    """B3: the circles label propagation found over the whole archive.

    Beside the recurring groups and not instead of them, because the two are
    different findings that look alike in a table. A group is one exact set of
    people a message was addressed to; a circle is a partition of the
    co-addressing graph, so two of its members need never have shared a single
    message. The listing is ordered by the mail that circulates in a circle
    rather than by how many people are in it — a circle of forty who exchanged
    three mails is a directory.
    """
    return _panel(
        icon="network",
        title="Circles",
        hint=(
            "Groups of correspondents who write to the same people, found by "
            "label propagation over the whole co-addressing graph. The name is "
            "the commonest domain among the members, never one anybody invented."
        ),
        error=AnalyticsInsightsState.communities_error,
        loading=AnalyticsInsightsState.loading_communities,
        anything=AnalyticsInsightsState.communities,
        nothing="No circle came out above the minimum size.",
        body=scroll_table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Circle"),
                    mn.table.th("People"),
                    mn.table.th("Messages"),
                    mn.table.th("Seen"),
                    mn.table.th("Key"),
                    mn.table.th(""),
                ),
            ),
            mn.table.tbody(
                rx.foreach(AnalyticsInsightsState.communities, _community_row)
            ),
        ),
    )


def _important_row(row: ImportantMessageView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.text(row.subject, size="sm", line_clamp=2)),
        mn.table.td(mn.text(row.sender, size="sm"), style=ADDRESS_COLUMN),
        mn.table.td(
            mn.group(
                score_bar(row.score),
                mn.text(row.score_label, size="sm", fw=600),
                gap="xs",
                wrap="nowrap",
                align="center",
            ),
        ),
        mn.table.td(_word_chips(row.reasons)),
        mn.table.td(row.when),
        mn.table.td(_graph_pill(MESSAGE_LINK, row.id)),
    )


def important_card() -> rx.Component:
    """B2: what probably matters, and why each row is claimed to.

    The reasons are a column and not a tooltip. The whole argument for scoring
    importance arithmetically rather than asking a model is that every term can
    be named, and a bar with the terms hidden behind a hover is a ranking a
    reader cannot argue with — which is the thing §1.1 refused.
    """
    return _panel(
        icon="flame",
        title="What probably matters",
        hint=(
            "A weighted sum over headers a person can check: who replied, how "
            "many did, whether you were addressed directly, whether the text "
            "looks automated. Deterministic, and recomputed by every rebuild."
        ),
        error=AnalyticsInsightsState.important_error,
        loading=AnalyticsInsightsState.loading_important,
        anything=AnalyticsInsightsState.important,
        nothing="Nothing has been scored yet — a rebuild writes these.",
        body=scroll_table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Subject"),
                    mn.table.th("From"),
                    mn.table.th("Importance"),
                    mn.table.th("Why"),
                    mn.table.th("Sent"),
                    mn.table.th(""),
                ),
            ),
            mn.table.tbody(
                rx.foreach(AnalyticsInsightsState.important, _important_row)
            ),
        ),
    )


def _tag_line(tag: TagView) -> rx.Component:
    """One tag: what it is, what it holds, what it is offered, and two verbs.

    "Accept all" appears only where something is actually being offered, and
    what stands in its place is a sentence rather than a nought — a disabled
    button with a zero beside it reads as a broken analysis, and R8 says the
    honest reading is "no rebuild has run since this tag was made".
    """
    return mn.group(
        label_chip(tag.name, rx.cond(tag.color != "", tag.color, DEFAULT_TAG_COLOR)),
        mn.group(
            mn.text(tag.message_count, size="xs", c="dimmed", class_name="ma-tabular"),
            rx.cond(
                tag.suggestions > 0,
                pill_action(
                    f"Accept {tag.suggestions} suggested",
                    icon="check-check",
                    on_click=AnalyticsInsightsState.accept_all(tag.id),
                    loading=AnalyticsInsightsState.tagging,
                ),
                mn.text("nothing suggested", size="xs", c="dimmed"),
            ),
            _graph_pill(TAG_LINK, tag.id),
            pill_action(
                "Delete",
                icon="trash-2",
                on_click=AnalyticsInsightsState.delete_tag(tag.id),
            ),
            gap=6,
            align="center",
            wrap="nowrap",
        ),
        justify="space-between",
        w="100%",
        wrap="nowrap",
    )


def rebuild_hint() -> rx.Component:
    """R8, said out loud, with the button that ends the wait.

    ``SUGGESTED`` is derived: every rebuild deletes the edges and writes them
    again, so a tag made between two rebuilds is offered nothing at all until
    another one runs. A card that showed only zeroes would read as an analysis
    that failed, which is why this says which of the two it is and puts the
    rebuild next to the sentence rather than at the top of the page.
    """
    return message(
        mn.stack(
            mn.text(
                "Nothing is being suggested for these tags yet. Suggestions are "
                "written by a rebuild and deleted by the next one, so a tag made "
                "since the last rebuild has none until another runs.",
                size="xs",
            ),
            soft_button(
                "Rebuild the derived layer",
                on_click=AnalyticsInsightsState.start_rebuild,
                loading=AnalyticsInsightsState.starting,
                disabled=~AnalyticsInsightsState.can_rebuild,
                left_section=rx.icon("play", size=14),
                size="xs",
            ),
            gap="xs",
            align="flex-start",
        ),
        "note",
    )


def tags_card() -> rx.Component:
    """The annotation layer: the one label a rebuild never recomputes.

    Its own card on this page rather than
    :func:`~mailarc_ui.tags.tags_panel`, which is the explorer's: that one
    stands beside a canvas and opens a tag's offers in the column next to it,
    and this one is a listing among listings with the two bulk verbs on the
    row. Both drive the same mixin, so a tag deleted here is deleted there.
    """
    return _panel(
        icon="tag",
        title="Tags",
        hint=(
            "What somebody named, and what an analysis thinks belongs to it. A "
            "tag survives every rebuild; the suggestions beside it do not — "
            "each rebuild deletes them and works them out again."
        ),
        error=AnalyticsInsightsState.tag_error,
        loading=AnalyticsInsightsState.loading_tags,
        anything=AnalyticsInsightsState.tags,
        nothing="No tags yet. Promote a topic or a circle in the graph explorer.",
        body=mn.stack(
            rx.foreach(AnalyticsInsightsState.tags, _tag_line),
            rx.cond(
                AnalyticsInsightsState.tags_await_a_rebuild,
                rebuild_hint(),
                mn.text(""),
            ),
            gap="xs",
            w="100%",
        ),
    )


def _template_row(row: TemplateView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(
            mn.group(
                score_bar(row.score),
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
    return scroll_table(
        mn.table.thead(
            mn.table.tr(
                mn.table.th("Automatable"),
                mn.table.th("Times"),
                mn.table.th("Sample"),
                mn.table.th("Seen"),
            ),
        ),
        mn.table.tbody(rx.foreach(rows, _template_row)),
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


def _panel_error(text: rx.Var | str) -> rx.Component:
    """What a panel shows instead of its table when the graph did not answer.

    Instead of, and never above: half a table under a red alert reads as data,
    and the numbers in it would be from whenever the last read succeeded.

    ``text`` rather than ``message``, which is what this parameter was called
    until :func:`~mailarc_ui.kit.message` arrived — a parameter that shadows
    the function it is about to call is a rename waiting to bite.
    """
    return message(text, "failure")


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
                    spinner(),
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
        empty_panel(
            "sparkles",
            "Nothing to analyse yet",
            AnalyticsInsightsState.guidance,
            rx.cond(
                AnalyticsInsightsState.needs_rebuild,
                primary_button(
                    "Rebuild now",
                    on_click=AnalyticsInsightsState.start_rebuild,
                    loading=AnalyticsInsightsState.starting,
                    disabled=~AnalyticsInsightsState.can_rebuild,
                    left_section=rx.icon("play", size=14),
                    size="sm",
                ),
                mn.text(""),
            ),
        ),
    )


def analyses() -> rx.Component:
    """The cross-check across the page, then six listings in two columns.

    The page fills the window, and cards stacked down a 1300px column are
    tables with more white to their right than table. What decides which card
    goes where is how wide its widest row is. The cross-check carries two
    address columns beside three more and gets the whole width. The wide column
    under it takes the three listings whose rows are *prose* — a subject and
    its reasons, a topic and its keywords, a sample of a template's text — and
    the narrow one the three that are a name and a handful of numbers: tags,
    circles, groups.

    Columns rather than a grid of rows, for the reason the dashboard states:
    a row is as tall as its tallest cell, and these listings are twelve rows
    or three. Two columns have nothing to align, so each closes up and the
    slack lands at the foot of the shorter one.

    Sceptical panel first either way — it is the only one that can say
    something is *wrong* rather than report what was found, and it stays above
    the findings that depend on its answer.
    """
    return mn.stack(
        agreement_card(),
        mn.grid(
            mn.grid_col(
                mn.stack(
                    important_card(),
                    topics_card(),
                    templates_card(),
                    gap="md",
                    w="100%",
                ),
                span={"base": 12, "lg": 7},
            ),
            mn.grid_col(
                mn.stack(
                    tags_card(),
                    communities_card(),
                    groups_card(),
                    gap="md",
                    w="100%",
                ),
                span={"base": 12, "lg": 5},
            ),
            gutter="md",
            w="100%",
        ),
        gap="md",
        w="100%",
    )


def insights_panel() -> rx.Component:
    """The whole page's body, for a page to drop in.

    Grows with its content rather than scrolling inside itself, and a panel
    that owned its own height would collapse to nothing the moment a parent
    did not have a definite one. The scrolling that does happen belongs to the
    listings: each caps itself at twelve rows, so the page stays the height of
    its cards however much the analyses found.

    Owns the ``on_unmount`` because it owns the rebuild card that starts the
    poll. Without it a user who navigates away mid-rebuild leaves a background
    task asking the database every two seconds for the rest of the session —
    one per abandoned page. ``stop_polling`` was written for this and had no
    caller anywhere in the repository.
    """
    return mn.stack(
        rebuild_card(),
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
