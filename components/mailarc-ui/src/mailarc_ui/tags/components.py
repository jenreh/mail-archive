"""The three things a page draws of the annotation layer.

Each takes the concrete state class rather than reading one — the mixin is
copied into every host, so ``tag_chips(GraphExplorerState, …)`` and, in phase 5,
``tag_chips(AnalyticsInsightsState, …)`` are two different sets of vars behind
one drawing. The same contract :mod:`mailarc_ui.message_detail.components`
keeps.

Nothing here builds a design element of its own: the chip, the pill, the bar and
the table all come out of :mod:`mailarc_ui.kit`, and the form says what is wrong
with it through Mantine's own ``error`` — the box a person has to fix, rather
than a red alert somewhere above it.
"""

from __future__ import annotations

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import (
    input_field,
    label_chip,
    panel_card,
    pill_action,
    primary_button,
    score_bar,
    scroll_table,
)
from mailarc_ui.tags.model import PROMOTE_FIELD, SuggestionView, TagView
from mailarc_ui.tags.state import TagActionsState

DEFAULT_TAG_COLOR = "gray.6"
"""What a tag nobody coloured wears.

A palette key rather than a hex, like every other chip in this application: the
one place in the archive that has to hand out concrete hexes is the graph
canvas, because cytoscape paints where no stylesheet can reach.
"""

TAG_ROWS = 8
"""How many suggestions a panel shows before it scrolls. Shorter than a listing
because this table sits under a message rather than filling a page."""


def tag_chips(state: type[TagActionsState], message_id: rx.Var | str) -> rx.Component:
    """What one message wears, and the menu that adds to it.

    The chips are what the *message* carries and the menu is every tag the
    archive holds — two different lists, which is why the state reads them
    separately rather than filtering one out of the other.
    """
    return mn.group(
        rx.foreach(
            state.message_tags,
            lambda tag: _worn_chip(state, tag, message_id),
        ),
        _add_menu(state, message_id),
        gap=6,
        align="center",
        wrap="wrap",
    )


def promote_form(
    state: type[TagActionsState],
    kind: rx.Var | str,
    cluster_id: rx.Var | str,
) -> rx.Component:
    """Name a cluster, and it becomes a tag its messages wear.

    ``kind`` and ``cluster_id`` are passed in rather than read off the state
    because the two hosts hold them differently — a selected node here, a table
    row there — and because the handler behind the button takes exactly those
    two arguments. What the *mixin* owns is the name and its complaint.

    The origin the tag ends up with is the kind and never the id: a cluster id
    is a digest of its members and is minted afresh by every rebuild (R7), so
    the tag is the durable reference the cluster is not — which is the sentence
    under the field.
    """
    return mn.stack(
        _promote_field(state),
        primary_button(
            "Promote to a tag",
            left_section=rx.icon("tag", size=14),
            on_click=state.promote(kind, cluster_id),
            loading=state.tagging,
            disabled=state.has_errors,
            size="xs",
        ),
        gap="xs",
        w="100%",
    )


def suggestion_rows(state: type[TagActionsState]) -> rx.Component:
    """What one tag is being offered, and the two ways of taking it.

    Says which kind of group made the case, because "these two answer each
    other" and "these two are in the same circle" are not the same claim and
    somebody accepting one should see which was made.
    """
    return mn.stack(
        mn.group(
            mn.text(
                "Suggested for this tag",
                size="xs",
                c="dimmed",
                class_name="ma-field-label",
            ),
            pill_action(
                "Accept all",
                icon="check-check",
                on_click=state.accept_all(state.suggestion_tag),
                loading=state.tagging,
            ),
            justify="space-between",
            w="100%",
        ),
        rx.cond(
            state.has_suggestions,
            _suggestion_table(state),
            mn.text(
                "Nothing is being suggested for this tag. Suggestions are "
                "written by a rebuild, so a tag made since the last one has "
                "none yet.",
                size="xs",
                c="dimmed",
            ),
        ),
        gap="xs",
        w="100%",
    )


def tags_panel(state: type[TagActionsState]) -> rx.Component:
    """Every tag in the archive, with what each is being offered.

    A card rather than a bare list because it is the one place a tag can be
    deleted from, and a destructive action loose on a page is one nobody
    expects.
    """
    return panel_card(
        mn.stack(
            mn.text("Tags", size="sm", fw=600),
            rx.cond(
                state.has_tags,
                mn.stack(
                    rx.foreach(state.tags, lambda tag: _tag_row(state, tag)),
                    gap=4,
                    w="100%",
                ),
                mn.text(
                    "No tags yet. Promote a topic or a circle to make one.",
                    size="xs",
                    c="dimmed",
                ),
            ),
            gap="xs",
        ),
    )


def _promote_field(state: type[TagActionsState]) -> rx.Component:
    """The name box, wearing its own complaint.

    ``error`` and not an alert over the form: the control already knows how to
    be invalid — a red border, ``aria-invalid`` on the input a screen reader is
    on, and the message under the box a person has to fix.
    """
    return input_field(
        "Tag name",
        description=(
            "The tag is what survives the next rebuild; the cluster's own id does not."
        ),
        value=state.promote_name,
        on_change=state.set_promote_name,
        error=state.errors[PROMOTE_FIELD],
        placeholder="NORD-42",
        size="xs",
    )


def _worn_chip(
    state: type[TagActionsState], tag: TagView, message_id: rx.Var | str
) -> rx.Component:
    """One tag this message wears, with the way to take it off."""
    return mn.group(
        label_chip(
            tag.name,
            rx.cond(tag.color != "", tag.color, DEFAULT_TAG_COLOR),
        ),
        mn.unstyled_button(
            rx.icon("x", size=12),
            on_click=state.untag_message(tag.id, message_id),
            aria_label="Remove this tag",
            class_name="ma-chip-remove",
        ),
        gap=2,
        align="center",
        wrap="nowrap",
    )


def _add_menu(state: type[TagActionsState], message_id: rx.Var | str) -> rx.Component:
    """Every tag in the archive, as somewhere to file this message.

    The trigger's label is a constant on purpose: Reflex compiles an element
    carrying a state-dependent attribute into a ``memo`` wrapper that keeps
    children and discards everything else, and ``Menu.Target`` opens its
    dropdown by cloning that child — so a Var on the trigger makes the menu
    silently unopenable.
    """
    return mn.menu(
        mn.menu.target(pill_action("Tag", icon="tag")),
        mn.menu.dropdown(
            rx.cond(
                state.has_tags,
                rx.foreach(
                    state.tags,
                    lambda tag: mn.menu.item(
                        tag.name,
                        on_click=state.tag_message(tag.id, message_id),
                    ),
                ),
                mn.menu.item("No tags yet", disabled=True),
            ),
        ),
        position="bottom-start",
        shadow="md",
        width=220,
    )


def _tag_row(state: type[TagActionsState], tag: TagView) -> rx.Component:
    """One tag: its name, what it holds, what it is offered, and Delete."""
    return mn.group(
        label_chip(tag.name, rx.cond(tag.color != "", tag.color, DEFAULT_TAG_COLOR)),
        mn.group(
            mn.text(tag.message_count, size="xs", c="dimmed", class_name="ma-tabular"),
            rx.cond(
                tag.suggestions > 0,
                pill_action(
                    f"{tag.suggestions} suggested",
                    on_click=state.show_suggestions(tag.id),
                ),
                rx.fragment(),
            ),
            mn.unstyled_button(
                rx.icon("trash-2", size=14),
                on_click=state.delete_tag(tag.id),
                aria_label="Delete this tag",
                class_name="ma-chip-remove",
            ),
            gap=6,
            align="center",
            wrap="nowrap",
        ),
        justify="space-between",
        w="100%",
        wrap="nowrap",
    )


def _suggestion_table(state: type[TagActionsState]) -> rx.Component:
    return scroll_table(
        mn.table.thead(
            mn.table.tr(
                mn.table.th("Message"),
                mn.table.th("Case"),
                mn.table.th("Sent"),
                mn.table.th(""),
            ),
        ),
        mn.table.tbody(
            rx.foreach(state.suggestions, lambda row: _suggestion_row(state, row)),
        ),
        rows=TAG_ROWS,
    )


def _suggestion_row(state: type[TagActionsState], row: SuggestionView) -> rx.Component:
    return mn.table.tr(
        mn.table.td(mn.text(row.subject, size="sm", line_clamp=1)),
        mn.table.td(
            mn.group(
                score_bar(row.score),
                mn.text(row.score_label, size="xs", class_name="ma-tabular"),
                mn.text(row.method, size="xs", c="dimmed"),
                gap=6,
                wrap="nowrap",
                align="center",
            ),
        ),
        mn.table.td(mn.text(row.when, size="xs", c="dimmed")),
        mn.table.td(
            pill_action(
                "Accept",
                on_click=state.accept_suggestion(state.suggestion_tag, row.message_id),
            ),
        ),
    )


__all__ = [
    "DEFAULT_TAG_COLOR",
    "TAG_ROWS",
    "promote_form",
    "suggestion_rows",
    "tag_chips",
    "tags_panel",
]
