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
    group_chevron,
    group_header,
    label_chip,
    list_row,
    pill_icon_action,
    quiet_button,
    range_select,
    relevance_chip,
    spinner,
)
from mailarc_ui.message_detail import COLUMN, ROW_BORDER, LabelChip, message_tabs
from mailarc_ui.search.form import FORM_WIDTH, search_form
from mailarc_ui.search.model import GROUPING_OPTIONS, ListLine
from mailarc_ui.search.state import MailSearchState
from mailarc_ui.shell import routes

PAPERCLIP = "paperclip"
"""What a row wears when it carries files."""

CONVERSATION = "messages-square"
"""What a heading wears beside how many messages the conversation holds."""

LIST_MIN_WIDTH = 280
"""Narrow enough to give the message room, wide enough for a subject line."""

LIST_MAX_WIDTH = 620
"""Past this the list is a page of its own and the message is a column."""

CHIP_GAP = 6

GROUP_BY = "Group by"
"""What the dropdown over the list is for, said beside it."""

GROUPING_WIDTH = 176
"""Wide enough for *Conversation / Thread*, the longest thing it can say."""

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


def _attachment_pill(line: ListLine) -> rx.Component:
    """The paperclip, with a number beside it when the archive knows one.

    It usually does not: a summary answers *whether* a message carries files
    and not how many, so the pill shows the glyph alone. See
    :attr:`~mailarc_ui.search.model.ResultRow.attachment_count`.
    """
    return rx.cond(
        line.attachment_count > 0,
        count_chip(PAPERCLIP, line.attachment_count),
        count_chip(PAPERCLIP, ""),
    )


def _chips(line: ListLine) -> rx.Component:
    """The pill row under the preview: files, labels, and how well it ranked.

    On a heading the conversation's size joins them, first, because it is what
    the line is *about* — the rest describe the message it happens to show.
    """
    return mn.group(
        rx.cond(
            line.size_label != "",
            count_chip(CONVERSATION, line.size_label),
            rx.fragment(),
        ),
        rx.cond(line.has_attachments, _attachment_pill(line), rx.fragment()),
        rx.foreach(line.labels, _label_pill),
        rx.cond(
            line.relevance_label != "",
            relevance_chip(line.relevance_label),
            rx.fragment(),
        ),
        rx.cond(line.can_expand, _whole_pill(line), rx.fragment()),
        gap=CHIP_GAP,
        wrap="wrap",
        pt=2,
    )


def _sender_line(line: ListLine) -> rx.Component:
    """Who it is from, and how long ago — ``Anna Bauer · 9m``."""
    return mn.group(
        mn.text(
            line.sender,
            size="md",
            fw=500,
            truncate="end",
            style={"minWidth": 0},
        ),
        rx.cond(
            line.when_label != "",
            mn.group(
                mn.text("·", size="sm", c="dimmed"),
                mn.text(line.when_label, size="sm", c="dimmed"),
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


def _graph_pill(line: ListLine) -> rx.Component:
    """Take this message to the explorer, rooted at itself.

    ``rx.redirect`` from a button rather than an ``rx.link`` around one: the
    row is already clickable, and an anchor inside it is the nested-anchor
    hydration error the rail exists to keep out of this application.
    """
    return pill_icon_action(
        icon="waypoints",
        label="Show in graph",
        on_click=rx.redirect(GRAPH_LINK + line.id),
    )


def _whole_pill(line: ListLine) -> rx.Component:
    """Fetch the members this answer left out, for this one conversation.

    Its own loading flag and never the list's: asking one conversation for the
    rest of itself is not a search, and putting the page-wide spinner up for it
    would take the Search button away while it ran.
    """
    return pill_icon_action(
        icon="messages-square",
        label="Show whole conversation",
        loading=line.busy,
        on_click=MailSearchState.show_whole_conversation(
            line.group_id
        ).stop_propagation,
    )


def _body(line: ListLine) -> rx.Component:
    """Everything a line says about the message it is showing."""
    return mn.stack(
        _sender_line(line),
        mn.text(line.subject, size="md", fw=600, truncate="end", w="100%"),
        mn.text(line.preview, size="sm", c="dimmed", truncate="end", w="100%"),
        _chips(line),
        gap=2,
        style={"minWidth": 0, "flex": "1 1 auto"},
    )


def _message_row(line: ListLine) -> rx.Component:
    """One hit: the sender's initials, and everything the row says about it."""
    return list_row(
        avatar_initials(line.initials),
        _body(line),
        _graph_pill(line),
        selected=MailSearchState.selected_id == line.id,
        on_click=lambda: MailSearchState.select(line.id),
        custom_attrs={"data-child": rx.cond(line.indented, "true", "false")},
        w="100%",
    )


def _group_row(line: ListLine) -> rx.Component:
    """A conversation's heading, which is the newest message it holds.

    The same row, with a chevron in front. Two gestures on one line and they
    stay separable: the row opens the message it is showing, the chevron opens
    the conversation — which is what every mail client that groups does, and
    what makes a closed group hide nothing a reader had already been shown.
    """
    return list_row(
        group_chevron(
            expanded=line.expanded,
            on_click=MailSearchState.toggle_group(line.group_id).stop_propagation,
        ),
        avatar_initials(line.initials),
        _body(line),
        _graph_pill(line),
        selected=MailSearchState.selected_id == line.id,
        on_click=lambda: MailSearchState.select(line.id),
        w="100%",
    )


def _section_row(line: ListLine) -> rx.Component:
    """A section over a group that is not a conversation — a sender, a topic.

    Not a message row: there is nothing to open behind it, so the whole line
    is the one gesture, and it opens or closes the group. Which is why the
    chevron inside it carries no handler of its own.
    """
    return group_header(
        line.label,
        line.size_label,
        expanded=line.expanded,
        on_click=MailSearchState.toggle_group(line.group_id),
    )


def _list_line(line: ListLine) -> rx.Component:
    """One line of the list, whichever of the three kinds it is."""
    return rx.cond(
        line.is_section,
        _section_row(line),
        rx.cond(line.is_header, _group_row(line), _message_row(line)),
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


def _grouping_select() -> rx.Component:
    """What the list is grouped by — the one thing this strip decides.

    ``range_select`` and not a field: it changes what the panel is showing and
    saves nothing, which is exactly what that control is for. The options are
    a constant because the archive always offers all eight; a grouping whose
    read has nothing to say — a topic on an archive nobody has rebuilt — draws
    one bucket rather than going grey.
    """
    return mn.group(
        mn.text(GROUP_BY, size="xs", c="dimmed"),
        range_select(
            value=MailSearchState.grouping,
            data=GROUPING_OPTIONS,
            on_change=MailSearchState.choose_grouping,
            aria_label=GROUP_BY,
            w=GROUPING_WIDTH,
        ),
        gap=CHIP_GAP,
        align="center",
        wrap="nowrap",
    )


def _count_strip() -> rx.Component:
    """How many of how many, over the list — and how the list is grouped."""
    return mn.group(
        mn.text(
            MailSearchState.count_label,
            size="xs",
            c="dimmed",
            class_name="ma-tabular",
        ),
        _grouping_select(),
        justify="space-between",
        align="center",
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
                        rx.foreach(MailSearchState.lines, _list_line),
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
