"""The reading pane: one mail, rendered the way a client renders it, or raw.

Layout and nothing else. Every value comes from the state class handed in, and
that is the whole trick of this module — each function takes the *concrete*
state as an argument rather than naming one, so the same pane serves the search
page and the review page while each keeps its own selection. The argument is a
class, not an instance: a component is built once at compile time and what it
holds are that class's Vars.

The rendered HTML lives in an ``<iframe sandbox>``: no scripts, no forms, no
navigation, no same-origin access, and the document it loads carries a CSP that
allows nothing remote. That is the whole defence against a hostile mail, and it
is the browser's, not a tag allow-list of ours.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import attachment_card, avatar_initials
from mailarc_ui.message_detail.model import (
    TAB_MESSAGE,
    TAB_SOURCE,
    AttachmentRow,
)
from mailarc_ui.message_detail.state import MessageDetailState

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


def _header_line(label: str, value: rx.Var | str) -> rx.Component:
    """One ``From:``-style line of the header block."""
    return mn.group(
        mn.text(label, size="sm", c="dimmed", w=48, style={"flexShrink": 0}),
        mn.text(value, size="sm", style={"wordBreak": "break-word"}),
        gap="xs",
        wrap="nowrap",
        align="flex-start",
    )


ATTACHMENT_WIDTH = 240
"""How wide one attachment card is allowed to grow, in pixels.

Bounded rather than content-sized so that three files read as three cards in a
row instead of one long ribbon; the filename ellipses inside the card.
"""


def _attachment_card(row: AttachmentRow) -> rx.Component:
    """One file on the card the kit offers files on."""
    return attachment_card(row.filename, row.size_label, maw=ATTACHMENT_WIDTH)


def message_header(state: type[MessageDetailState]) -> rx.Component:
    """Who wrote it, when, about what, and what came with it.

    The order a mail client uses and not the order a header block has: the
    sender is a person — initials, name, address — with the date opposite,
    and the subject is the largest thing on the page under them. ``To`` and
    ``Cc`` stay as labelled lines below, because they answer a question that
    is asked occasionally rather than every time.
    """
    return mn.stack(
        mn.group(
            avatar_initials(state.view.sender, size=44),
            mn.stack(
                mn.text(state.view.sender, class_name="ma-reading-sender"),
                mn.text(state.view.sender_address, class_name="ma-reading-address"),
                gap=2,
                style={"minWidth": 0, "flex": "1 1 0%"},
            ),
            mn.text(state.view.date, class_name="ma-reading-date"),
            gap=12,
            align="flex-start",
            wrap="nowrap",
        ),
        mn.title(
            state.view.subject,
            order=3,
            line_clamp=2,
            class_name="ma-reading-subject",
        ),
        mn.stack(
            _header_line("To", state.view.recipients),
            rx.cond(
                state.has_cc,
                _header_line("Cc", state.view.cc),
                mn.text(""),
            ),
            gap=2,
        ),
        rx.cond(
            state.has_attachments,
            mn.group(
                rx.foreach(state.view.attachments, _attachment_card),
                gap="xs",
            ),
            mn.text(""),
        ),
        gap="sm",
        class_name="ma-reading-header",
    )


def remote_content_bar(state: type[MessageDetailState]) -> rx.Component:
    """The question a mail client asks before it fetches anything remote."""
    return mn.alert(
        mn.group(
            mn.text(state.remote_notice, size="sm", flex="1"),
            mn.button(
                "Allow once",
                on_click=state.allow_remote_once,
                variant="light",
                size="xs",
            ),
            mn.button(
                "Allow for this sender",
                on_click=state.allow_remote_for_sender,
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


def message_body(state: type[MessageDetailState]) -> rx.Component:
    """The sender's HTML in a sandbox, or the text body when there is none."""
    return rx.cond(
        state.has_html_body,
        rx.el.iframe(
            src_doc=state.frame_html,
            sandbox="",
            referrer_policy="no-referrer",
            title="Message body",
            style={
                **GROW,
                "width": "100%",
                "border": "0",
                # The frame element is in *this* document, so a token resolves
                # here normally. The document inside it is another matter — see
                # `message_detail/model.py`'s FRAME_STYLE.
                "background": "var(--ma-surface)",
                "borderRadius": "var(--mantine-radius-sm)",
            },
        ),
        mn.scroll_area(
            mn.text(
                state.view.body_text,
                class_name="ma-reading-body",
                style={"whiteSpace": "pre-wrap", "wordBreak": "break-word"},
            ),
            type="hover",
            style=GROW,
        ),
    )


def message_view(state: type[MessageDetailState]) -> rx.Component:
    """The readable tab: header block on top, body filling the rest."""
    return mn.stack(
        message_header(state),
        rx.cond(state.remote_blocked, remote_content_bar(state), mn.text("")),
        rx.cond(
            state.message_note != "",
            mn.text(state.message_note, size="xs", c="dimmed"),
            mn.text(""),
        ),
        mn.box(message_body(state), style=COLUMN),
        gap="sm",
        style=GROW,
    )


def raw_message_view(state: type[MessageDetailState]) -> rx.Component:
    """The source tab: the chosen message as it came off the wire."""
    return mn.stack(
        rx.cond(
            state.raw_truncated,
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
                state.raw,
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


def message_tabs(state: type[MessageDetailState]) -> rx.Component:
    """The right column: Message and Source, or a prompt to pick something."""
    return rx.cond(
        state.has_selection,
        rx.cond(
            state.loading_raw,
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
                _tab_panel(TAB_MESSAGE, message_view(state)),
                _tab_panel(TAB_SOURCE, raw_message_view(state)),
                value=state.tab,
                on_change=state.show_tab,
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
