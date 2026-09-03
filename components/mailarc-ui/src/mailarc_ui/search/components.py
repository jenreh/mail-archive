"""The search panel: ask on the left, the hits in the middle, the mail on the right.

Layout and nothing else. Every value comes from :class:`MailSearchState`, and
every piece of a row is a ``kit`` component — the initials circle, the pills,
the selectable row shell — so this mail list is drawn from the same vocabulary
as every other list in the archive.

The right column is :func:`~mailarc_ui.message_detail.message_tabs` handed
*this* page's state class. That is the whole reason the reading pane is
parameterised: filling a list and reading one of its rows are two jobs, and the
open message belongs to whichever page brought the list.

Three columns and not two, because a search is a question and a question needs
somewhere to be written. Each of them scrolls on its own, which is why the
page that drops this in has to give it a definite height.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import (
    COLUMN_GAP,
    LIST_WIDTH,
    avatar_initials,
    column_card,
    count_chip,
    empty_panel,
    label_chip,
    list_row,
    pill_action,
    quiet_button,
    relevance_chip,
    spinner,
)
from mailarc_ui.message_detail import COLUMN, ROW_BORDER, LabelChip, message_tabs
from mailarc_ui.search.form import FORM_WIDTH, search_form
from mailarc_ui.search.model import ResultRow
from mailarc_ui.search.state import MailSearchState
from mailarc_ui.shell import routes

PAPERCLIP = "paperclip"
"""What a row wears when it carries files."""

LIST_MIN_WIDTH = 280
"""Narrow enough to give the message room, wide enough for a subject line."""

LIST_MAX_WIDTH = 620
"""Past this the list is a page of its own and the message is a column."""

CHIP_GAP = 6

GRAPH_LINK = f"{routes.GRAPH}?view=message&id="
"""The explorer, rooted at one message.

Built by concatenation rather than by a helper that both this page and the
insights page would import: the string is a route and a query, the route is a
constant, and a module in ``mailarc_ui`` that existed only to join the two
would be a layer over ``+``.

A message id is a *canonical* id and not a digest of anything derived, so this
link — unlike a topic's (R7) — stays good across every rebuild.
"""


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


def _graph_pill(row: ResultRow) -> rx.Component:
    """Take this message to the explorer, rooted at itself.

    ``rx.redirect`` from a button rather than an ``rx.link`` around one: the
    row is already clickable, and an anchor inside it is the nested-anchor
    hydration error the rail exists to keep out of this application.
    """
    return pill_action(
        "Show in graph",
        icon="waypoints",
        on_click=rx.redirect(GRAPH_LINK + row.id),
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
            style={"minWidth": 0, "flex": "1 1 auto"},
        ),
        _graph_pill(row),
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
        empty_panel(
            "search-x",
            "Nothing matched",
            "Try fewer words, a wider date range, or another mailbox.",
        ),
        empty_panel(
            "inbox",
            "Nothing archived yet",
            "Import a mailbox and its messages show up here.",
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
                        gap=0,
                    ),
                    _load_more(),
                    type="hover",
                    offset_scrollbars=False,
                    style={"height": "100%"},
                ),
                rx.cond(
                    MailSearchState.searching,
                    spinner(),
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
    """The three columns, each with an edge of its own.

    Three surfaces with canvas between them rather than one frame split by
    hairlines. What the design gets out of it is a middle column that reads as
    a thing: divided by rules, the list has square corners on the side the
    message is, and looks like the gutter between the form and the mail rather
    than the list of what was found.
    """
    return mn.flex(
        column_card(search_form(), w=FORM_WIDTH, style={"flexShrink": 0}),
        mn.splitter(
            mn.splitter.pane(
                column_card(result_list()),
                default_size=f"{LIST_WIDTH}px",
                min=f"{LIST_MIN_WIDTH}px",
                max=f"{LIST_MAX_WIDTH}px",
                style={"overflow": "hidden"},
            ),
            mn.splitter.pane(
                column_card(
                    message_tabs(MailSearchState),
                    padding="md",
                    style={**COLUMN, "minWidth": 0},
                ),
                # A *number* rather than a length, and the difference is the
                # whole sizing contract: Mantine reads a px string as a fixed
                # pane (`flex: 0 1 <size>`) and a number as a share of what is
                # left (`flex: <n> 1 0`). A pane given neither compiles to
                # `flex-basis: 0` with no grow and collapses to nothing — which
                # is exactly what the message pane did. So the list is fixed at
                # its default width and the message absorbs the window.
                default_size=1,
                style={"overflow": "hidden", "minWidth": 0},
            ),
            # The resizer *is* the gap: at `COLUMN_GAP` wide with a
            # transparent face it draws nothing of its own, so the canvas
            # between the two cards is what the pointer grabs. Mantine's
            # handle — the small pill — is the only mark that it can be
            # dragged, and it earns its place: a seam that resizes and does
            # not say so is a seam nobody finds.
            line_size=COLUMN_GAP,
            handle_color="transparent",
            with_handle=True,
            h="100%",
            style={"flex": "1 1 0%", "minWidth": 0},
        ),
        gap=COLUMN_GAP,
        h="100%",
        w="100%",
        align="stretch",
    )
