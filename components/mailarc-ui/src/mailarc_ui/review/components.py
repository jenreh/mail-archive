"""The review panel: a mail-client list on the left, the chosen mail on the right.

Layout and nothing else. Every value comes from :class:`MessageReviewState`;
nothing here opens a graph or reads a file. The list row copies what a desktop
mail client shows — sender and date on one line, subject and a paperclip on
the next, two lines of preview under it, the labels it wears as small chips
under that — because that is the shape a human already knows how to scan.

The right half is :func:`~mailarc_ui.message_detail.message_tabs`, handed this
page's state class: the message the way a client renders it, and its source the
way it came off the wire. It is a shared component because the search page
reads a mail the same way — this module owns the list beside it, nothing more.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.message_detail import COLUMN, ROW_BORDER, message_tabs
from mailarc_ui.review.state import LabelChip, MessageReviewState, MessageRow

LIST_WIDTH = 360
"""The left column, in pixels. Wide enough for a sender and a date."""

SELECTED_BACKGROUND = "var(--mantine-color-blue-light)"
"""Mantine's own light tint, so the highlight follows the colour scheme."""


def _label_chip(chip: LabelChip) -> rx.Component:
    return mn.badge(
        chip.text,
        color=chip.color,
        variant="light",
        size="xs",
        radius="sm",
        tt="none",
        fw=500,
        style={"maxWidth": "100%"},
    )


def _row_labels(row: MessageRow) -> rx.Component:
    """The chips under the preview; nothing at all when there are none."""
    return rx.cond(
        row.labels,
        mn.group(rx.foreach(row.labels, _label_chip), gap=4, pt=2),
        mn.text(""),
    )


def _message_row(row: MessageRow) -> rx.Component:
    """One message, clickable, lit while it is the chosen one."""
    return mn.unstyled_button(
        mn.stack(
            mn.group(
                mn.text(row.sender, fw=700, size="sm", truncate="end", flex="1"),
                mn.text(
                    row.date_label,
                    size="sm",
                    c="dimmed",
                    class_name="ma-tabular",
                    style={"flexShrink": 0},
                ),
                gap="sm",
                wrap="nowrap",
                justify="space-between",
                w="100%",
            ),
            mn.group(
                mn.text(row.subject, size="sm", truncate="end", flex="1"),
                rx.cond(
                    row.has_attachments,
                    rx.icon("paperclip", size=14, color="var(--mantine-color-dimmed)"),
                    mn.text(""),
                ),
                gap="xs",
                wrap="nowrap",
                w="100%",
            ),
            mn.text(row.preview, size="sm", c="dimmed", line_clamp=2),
            _row_labels(row),
            gap=2,
            w="100%",
        ),
        # ty cannot model reflex event-handler calls; suppress the false positive.
        on_click=lambda: MessageReviewState.select(row.id),  # ty: ignore[invalid-argument-type]
        w="100%",
        px="md",
        py="sm",
        bg=rx.cond(
            MessageReviewState.selected_id == row.id, SELECTED_BACKGROUND, "transparent"
        ),
        style={
            "borderBottom": ROW_BORDER,
            "textAlign": "left",
            "display": "block",
            "_hover": {"background": "var(--mantine-color-default-hover)"},
        },
    )


def _load_more() -> rx.Component:
    return rx.cond(
        MessageReviewState.has_more,
        mn.group(
            mn.button(
                "Load more",
                on_click=MessageReviewState.load_more,
                loading=MessageReviewState.loading,
                variant="subtle",
                size="xs",
            ),
            justify="center",
            py="sm",
        ),
        mn.text(""),
    )


def message_list() -> rx.Component:
    """The scrollable left column."""
    return rx.cond(
        MessageReviewState.has_messages,
        mn.scroll_area(
            rx.foreach(MessageReviewState.messages, _message_row),
            _load_more(),
            type="hover",
            # ScrollArea takes no Mantine style props; unknown kwargs become
            # CSS keys, so the height has to be spelled out as CSS.
            style={"height": "100%"},
        ),
        rx.cond(
            MessageReviewState.loading,
            mn.group(mn.loader(size="sm"), justify="center", py="xl"),
            mn.empty_state(
                icon=rx.icon("inbox", size=28),
                title="Nothing archived yet",
                description="Import a mailbox and its messages show up here.",
                align="center",
            ),
        ),
    )


def review_panel() -> rx.Component:
    """Both columns in one bordered frame, for a page to drop in."""
    return mn.paper(
        mn.flex(
            mn.box(
                message_list(),
                w=LIST_WIDTH,
                h="100%",
                style={"borderRight": ROW_BORDER, "flexShrink": 0},
            ),
            mn.box(
                message_tabs(MessageReviewState),
                h="100%",
                p="md",
                style={**COLUMN, "minWidth": 0},
            ),
            h="100%",
            w="100%",
            align="stretch",
        ),
        radius="md",
        with_border=True,
        w="100%",
        h="100%",
        style={"overflow": "hidden"},
    )
