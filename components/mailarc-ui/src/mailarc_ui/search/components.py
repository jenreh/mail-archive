"""The search panel: ask on the left, the hits in the middle, the mail on the right.

Layout and nothing else. Every value comes from :class:`MailSearchState`, and
every piece of a row is a ``kit`` component — the initials circle, the pills,
the selectable row shell — so the mail list on this page and the one on the
review page are the same list, drawn from the same vocabulary.

The right column is :func:`~mailarc_ui.message_detail.message_tabs` handed
*this* page's state class. That is the whole reason the reading pane is
parameterised: the two pages fill their list differently and read a message
identically, and each keeps its own open message rather than sharing one.

Three columns and not two, because a search is a question and a question needs
somewhere to be written. Each of them scrolls on its own, which is why the
page that drops this in has to give it a definite height.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import (
    avatar_initials,
    count_chip,
    label_chip,
    list_row,
    quiet_button,
    relevance_chip,
)
from mailarc_ui.message_detail import COLUMN, ROW_BORDER, LabelChip, message_tabs
from mailarc_ui.search.form import FORM_WIDTH, search_form
from mailarc_ui.search.model import ResultRow
from mailarc_ui.search.state import MailSearchState

LIST_WIDTH = 360
"""The middle column, in pixels. Wide enough for a sender and a relative time."""

PAPERCLIP = "paperclip"
"""What a row wears when it carries files."""

ROW_GAP = 4
"""Between one row card and the next — they are cards, not table rows."""

CHIP_GAP = 6


def _label_pill(chip: LabelChip) -> rx.Component:
    """One label the message wears, behind its coloured dot."""
    return label_chip(chip.text, chip.color)


def _attachment_pill(row: ResultRow) -> rx.Component:
    """The paperclip, with a number beside it when the archive knows one.

    It usually does not: a summary answers *whether* a message carries files
    and not how many, so the pill shows the glyph alone. See
    :attr:`~mailarc_ui.search.model.ResultRow.attachment_count`.
    """
    return rx.cond(
        row.attachment_count > 0,
        count_chip(PAPERCLIP, row.attachment_count),
        count_chip(PAPERCLIP, ""),
    )


def _chips(row: ResultRow) -> rx.Component:
    """The pill row under the preview: files, labels, and how well it ranked."""
    return mn.group(
        rx.cond(row.has_attachments, _attachment_pill(row), rx.fragment()),
        rx.foreach(row.labels, _label_pill),
        rx.cond(
            row.relevance_label != "",
            relevance_chip(row.relevance_label),
            rx.fragment(),
        ),
        gap=CHIP_GAP,
        wrap="wrap",
        pt=2,
    )


def _sender_line(row: ResultRow) -> rx.Component:
    """Who it is from, and how long ago — ``Anna Bauer · 9m``."""
    return mn.group(
        mn.text(
            row.sender,
            size="md",
            fw=500,
            truncate="end",
            style={"minWidth": 0},
        ),
        rx.cond(
            row.when_label != "",
            mn.group(
                mn.text("·", size="sm", c="dimmed"),
                mn.text(row.when_label, size="sm", c="dimmed"),
                gap=CHIP_GAP,
                wrap="nowrap",
                style={"flexShrink": 0},
            ),
            rx.fragment(),
        ),
        gap=CHIP_GAP,
        wrap="nowrap",
        w="100%",
    )


def _result_row(row: ResultRow) -> rx.Component:
    """One hit: the sender's initials, and everything the row says about it."""
    return list_row(
        avatar_initials(row.initials),
        mn.stack(
            _sender_line(row),
            mn.text(row.subject, size="md", fw=600, truncate="end", w="100%"),
            mn.text(row.preview, size="sm", c="dimmed", truncate="end", w="100%"),
            _chips(row),
            gap=2,
            # A flex child may not be narrower than its content unless it is
            # told it may, and without that the truncation never happens.
            style={"minWidth": 0, "flex": "1 1 auto"},
        ),
        selected=MailSearchState.selected_id == row.id,
        on_click=lambda: MailSearchState.select(row.id),
        w="100%",
    )


def _load_more() -> rx.Component:
    return rx.cond(
        MailSearchState.has_more,
        mn.group(
            quiet_button(
                "Load more",
                on_click=MailSearchState.load_more,
                loading=MailSearchState.searching,
                size="xs",
            ),
            justify="center",
            py="sm",
        ),
        rx.fragment(),
    )


def _count_strip() -> rx.Component:
    """How many of how many, over the list."""
    return mn.group(
        mn.text(
            MailSearchState.count_label,
            size="xs",
            c="dimmed",
            class_name="ma-tabular",
        ),
        justify="space-between",
        px="md",
        py="xs",
        w="100%",
        style={"borderBottom": ROW_BORDER, "flexShrink": 0},
    )


def _nothing() -> rx.Component:
    """The two empty lists, which mean opposite things.

    A search that matched nothing is an answer and says so; an archive that
    holds nothing is a state, and telling a reader to narrow their search
    would send them looking for a message that was never imported.
    """
    return rx.cond(
        MailSearchState.nothing_matched,
        mn.empty_state(
            icon=rx.icon("search-x", size=28),
            title="Nothing matched",
            description="Try fewer words, a wider date range, or another mailbox.",
            align="center",
        ),
        mn.empty_state(
            icon=rx.icon("inbox", size=28),
            title="Nothing archived yet",
            description="Import a mailbox and its messages show up here.",
            align="center",
        ),
    )


def result_list() -> rx.Component:
    """The middle column: the count, then the hits, then a way to see more."""
    return mn.stack(
        _count_strip(),
        mn.box(
            rx.cond(
                MailSearchState.has_rows,
                mn.scroll_area(
                    mn.stack(
                        rx.foreach(MailSearchState.rows, _result_row),
                        gap=ROW_GAP,
                        p="xs",
                    ),
                    _load_more(),
                    type="hover",
                    # ScrollArea takes no Mantine style props; unknown kwargs
                    # become CSS keys, so the height is spelled out as CSS.
                    style={"height": "100%"},
                ),
                rx.cond(
                    MailSearchState.searching,
                    mn.group(mn.loader(size="sm"), justify="center", py="xl"),
                    _nothing(),
                ),
            ),
            style=COLUMN,
        ),
        gap=0,
        h="100%",
        w="100%",
    )


def search_panel() -> rx.Component:
    """All three columns in one bordered frame, for a page to drop in."""
    return mn.paper(
        mn.flex(
            mn.box(
                search_form(),
                w=FORM_WIDTH,
                h="100%",
                style={"borderRight": ROW_BORDER, "flexShrink": 0},
            ),
            mn.box(
                result_list(),
                w=LIST_WIDTH,
                h="100%",
                style={"borderRight": ROW_BORDER, "flexShrink": 0},
            ),
            mn.box(
                message_tabs(MailSearchState),
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
