"""The review panel: a mail-client list on the left, the chosen mail on the right.

Layout and nothing else. Every value comes from :class:`MessageReviewState`;
nothing here opens a graph or reads a file. The list row copies what a desktop
mail client shows — sender and date on one line, subject and a paperclip on
the next, two lines of preview under it, the labels it wears as small chips
under that — because that is the shape a human already knows how to scan. The
right half has two tabs: the message the way a client renders it, and its
source the way it came off the wire.

The rendered HTML lives in an ``<iframe sandbox>``: no scripts, no forms, no
navigation, no same-origin access, and the document it loads carries a CSP
that allows nothing remote. That is the whole defence against a hostile mail,
and it is the browser's, not a tag allow-list of ours.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.review.state import (
    TAB_MESSAGE,
    TAB_SOURCE,
    AttachmentRow,
    LabelChip,
    MessageReviewState,
    MessageRow,
)

LIST_WIDTH = 360
"""The left column, in pixels. Wide enough for a sender and a date."""

SELECTED_BACKGROUND = "var(--mantine-color-blue-light)"
"""Mantine's own light tint, so the highlight follows the colour scheme."""

ROW_BORDER = "1px solid var(--mantine-color-default-border)"

GROW = {"flex": "1 1 0%", "minHeight": 0}
"""Take the space that is left and give it back when asked.

Flex sizing rather than ``height: 100%`` on purpose: a percentage only
resolves against a parent whose height is *definite*, and through a tab panel
and a stack it stops being that — an ``iframe`` then falls back to the
browser's 150 pixels and shows the top of a mail and nothing else. A flex item
with ``flex: 1`` is sized by its container's layout instead, all the way down.
"""

COLUMN = {**GROW, "display": "flex", "flexDirection": "column"}
"""A growing pane whose children stack and can grow the same way."""


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
                mn.text(row.date_label, size="sm", c="dimmed", style={"flexShrink": 0}),
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


def _header_line(label: str, value: rx.Var | str) -> rx.Component:
    """One ``From:``-style line of the header block."""
    return mn.group(
        mn.text(label, size="sm", c="dimmed", w=48, style={"flexShrink": 0}),
        mn.text(value, size="sm", style={"wordBreak": "break-word"}),
        gap="xs",
        wrap="nowrap",
        align="flex-start",
    )


def _attachment_chip(row: AttachmentRow) -> rx.Component:
    return mn.badge(
        f"{row.filename} · {row.size_label}",
        left_section=rx.icon("paperclip", size=12),
        variant="light",
        color="gray",
        size="lg",
        radius="sm",
        tt="none",
        fw=500,
    )


def message_header() -> rx.Component:
    """Subject, the people, the date, the files — what sits above a body."""
    return mn.stack(
        mn.title(MessageReviewState.view.subject, order=3, line_clamp=2),
        mn.stack(
            _header_line("From", MessageReviewState.view.sender),
            _header_line("To", MessageReviewState.view.recipients),
            rx.cond(
                MessageReviewState.has_cc,
                _header_line("Cc", MessageReviewState.view.cc),
                mn.text(""),
            ),
            _header_line("Date", MessageReviewState.view.date),
            gap=2,
        ),
        rx.cond(
            MessageReviewState.has_attachments,
            mn.group(
                rx.foreach(MessageReviewState.view.attachments, _attachment_chip),
                gap="xs",
            ),
            mn.text(""),
        ),
        gap="sm",
        pb="sm",
        style={"borderBottom": ROW_BORDER},
    )


def remote_content_bar() -> rx.Component:
    """The question a mail client asks before it fetches anything remote."""
    return mn.alert(
        mn.group(
            mn.text(MessageReviewState.remote_notice, size="sm", flex="1"),
            mn.button(
                "Allow once",
                on_click=MessageReviewState.allow_remote_once,
                variant="light",
                size="xs",
            ),
            mn.button(
                "Allow for this sender",
                on_click=MessageReviewState.allow_remote_for_sender,
                variant="subtle",
                size="xs",
            ),
            gap="sm",
            align="center",
            wrap="wrap",
        ),
        color="yellow",
        variant="light",
        py="xs",
        icon=rx.icon("shield", size=16),
    )


def message_body() -> rx.Component:
    """The sender's HTML in a sandbox, or the text body when there is none."""
    return rx.cond(
        MessageReviewState.has_html_body,
        rx.el.iframe(
            src_doc=MessageReviewState.frame_html,
            sandbox="",
            referrer_policy="no-referrer",
            title="Message body",
            style={
                **GROW,
                "width": "100%",
                "border": "0",
                "background": "#fff",
                "borderRadius": "var(--mantine-radius-sm)",
            },
        ),
        mn.scroll_area(
            mn.text(
                MessageReviewState.view.body_text,
                size="sm",
                style={"whiteSpace": "pre-wrap", "wordBreak": "break-word"},
            ),
            type="hover",
            style=GROW,
        ),
    )


def message_view() -> rx.Component:
    """The readable tab: header block on top, body filling the rest."""
    return mn.stack(
        message_header(),
        rx.cond(MessageReviewState.remote_blocked, remote_content_bar(), mn.text("")),
        rx.cond(
            MessageReviewState.message_note != "",
            mn.text(MessageReviewState.message_note, size="xs", c="dimmed"),
            mn.text(""),
        ),
        mn.box(message_body(), style=COLUMN),
        gap="sm",
        style=GROW,
    )


def raw_message_view() -> rx.Component:
    """The source tab: the chosen message as it came off the wire."""
    return mn.stack(
        rx.cond(
            MessageReviewState.raw_truncated,
            mn.alert(
                "Only the beginning is shown; the full original is on disk.",
                color="yellow",
                variant="light",
                py="xs",
            ),
            mn.text(""),
        ),
        mn.scroll_area(
            mn.code(
                MessageReviewState.raw,
                block=True,
                style={"whiteSpace": "pre-wrap", "wordBreak": "break-all"},
            ),
            type="hover",
            style=GROW,
        ),
        gap="sm",
        style=GROW,
    )


TABS_STYLES = {
    # Mantine 9 lays its Tabs out through these variables — the root is
    # `display: var(--tabs-display)` (block by default) and a panel is
    # `flex-grow: var(--tabs-panel-grow)` (unset) — and its stylesheet beats a
    # class of ours. The `styles` API writes inline, which nothing beats, so
    # the root becomes a growing column and the active panel takes the rest.
    "root": {
        "--tabs-display": "flex",
        "--tabs-flex-direction": "column",
        "--tabs-panel-grow": "1",
        **GROW,
    },
    "panel": COLUMN,
}
"""What makes a Mantine Tabs fill its parent and let the panel scroll."""


def _tab_panel(value: str, body: rx.Component) -> rx.Component:
    return mn.tabs.panel(body, value=value, pt="sm")


def message_tabs() -> rx.Component:
    """The right column: Message and Source, or a prompt to pick something."""
    return rx.cond(
        MessageReviewState.has_selection,
        rx.cond(
            MessageReviewState.loading_raw,
            mn.group(mn.loader(size="sm"), justify="center", py="xl"),
            mn.tabs(
                mn.tabs.list(
                    mn.tabs.tab(
                        "Message",
                        value=TAB_MESSAGE,
                        left_section=rx.icon("mail", size=14),
                    ),
                    mn.tabs.tab(
                        "Source",
                        value=TAB_SOURCE,
                        left_section=rx.icon("code", size=14),
                    ),
                ),
                _tab_panel(TAB_MESSAGE, message_view()),
                _tab_panel(TAB_SOURCE, raw_message_view()),
                value=MessageReviewState.tab,
                on_change=MessageReviewState.show_tab,
                keep_mounted=False,
                # `styles` is not a declared prop on the wrapper; through
                # `custom_attrs` it reaches Mantine as the real thing.
                custom_attrs={"styles": TABS_STYLES},
            ),
        ),
        mn.empty_state(
            icon=rx.icon("mail-open", size=28),
            title="Pick a message",
            description="It shows up here, readable or as its raw source.",
            align="center",
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
                message_tabs(),
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
