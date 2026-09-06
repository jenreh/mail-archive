"""The reading pane: one mail, rendered the way a client renders it, or raw.

Layout and nothing else. Every value comes from the state class handed in, and
that is the whole trick of this module — each function takes the *concrete*
state as an argument rather than naming one, so the same pane serves any page
that lists the mixin while each keeps its own selection. The argument is a
class, not an instance: a component is built once at compile time and what it
holds are that class's Vars.

The rendered HTML lives in an ``<iframe sandbox>``: no scripts, no forms, no
navigation, no same-origin access, and the document it loads carries a CSP that
allows nothing remote. That is the whole defence against a hostile mail, and it
is the browser's, not a tag allow-list of ours.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import (
    attachment_card,
    avatar_initials,
    docx_viewer,
    empty_panel,
    glyph_action,
    link_chip,
    message,
    pdf_viewer,
    pptx_viewer,
    quiet_button,
    slide_over,
    soft_button,
    spinner,
    xlsx_viewer,
)
from mailarc_ui.message_detail.model import (
    GRAPH_GLYPH,
    TAB_GRAPH,
    TAB_MESSAGE,
    TAB_SOURCE,
    AddressRow,
    AttachmentRow,
    ChipKind,
    MessageChip,
)
from mailarc_ui.message_detail.preview import PreviewKind
from mailarc_ui.message_detail.records import calendar_preview, contact_preview
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


def _graph_glyph(
    state: type[MessageDetailState],
    kind: str,
    root: rx.Var | str,
    label: rx.Var | str,
) -> rx.Component:
    """The little icon that opens one thing in the graph tab.

    The same gesture the chips make, in the shape a line of running text can
    hold: the subject, the sender and every recipient already say what they
    are, so what hangs beside them is one glyph that says what pressing it
    does. All of them open the third tab — there is one, and the last thing
    pressed is what it stands for.
    """
    return glyph_action(
        GRAPH_GLYPH,
        f"Show {label} in the graph",
        on_click=state.show_chip_graph(kind, root, label),
    )


def _address(state: type[MessageDetailState], row: AddressRow) -> rx.Component:
    """One person on a ``To:`` line, with the way into their own graph."""
    return mn.group(
        mn.text(row.label, class_name="ma-reading-recipients"),
        _graph_glyph(state, ChipKind.ADDRESS.value, row.address, row.label),
        gap=2,
        wrap="nowrap",
        align="center",
    )


def _recipient_line(
    state: type[MessageDetailState],
    label: str,
    # The class attribute is annotated with what the *state* holds and the
    # class exposes a Var over it, so both spellings are the truth depending
    # on which side you read from — the same seam `graph/components.py`
    # documents on `hidden_kinds`.
    rows: rx.Var | list[AddressRow],
) -> rx.Component:
    """One ``To:`` line of the header block, under the sender's name.

    The label runs inline in front of the addresses rather than in a column of
    its own: a mail client sets the whole line as one quiet sentence, and a
    fixed label column would indent the addresses away from the name they
    belong under. The label itself never shrinks, so a long recipient list
    wraps inside its own box and the ``To:`` stays put.

    The people wrap as entries rather than running together with commas,
    because each of them now carries a glyph and a comma between a glyph and
    the next name reads as punctuation belonging to the button.
    """
    return mn.group(
        mn.text(label, class_name="ma-reading-recipient-label"),
        mn.group(
            rx.foreach(rows, lambda row: _address(state, row)),
            gap=10,
            wrap="wrap",
            align="center",
            style={"minWidth": 0},
        ),
        gap=6,
        wrap="nowrap",
        align="flex-start",
    )


ATTACHMENT_WIDTH = 240
"""How wide one attachment card is allowed to grow, in pixels.

Bounded rather than content-sized so that three files read as three cards in a
row instead of one long ribbon; the filename ellipses inside the card.
"""


def _attachment_card(
    state: type[MessageDetailState], row: AttachmentRow, index: int
) -> rx.Component:
    """One file on the card the kit offers files on, pressed to open it.

    ``PDF · 591 KB`` under the name, and the family's own glyph on the tinted
    square: a reader scanning a strip of six files is looking for *the
    spreadsheet*, and six identical document glyphs make them read six
    filenames to find it. The two halves of the meta line are joined here
    rather than stored joined, because the panel's own heading needs the size
    without the family beside it.

    Addressed by position and not by name: two parts of one message routinely
    arrive called ``image001.png``, and a panel that opened whichever came
    first would be showing the wrong picture with no way to tell.

    ``index`` is annotated ``int`` and arrives as a ``Var`` — what ``rx.foreach``
    hands its second parameter — the same trade every item in this module makes:
    the annotation says what the value *is* in the browser, and typing it as a
    ``Var`` would only make the handler's own signature unassignable.
    """
    return attachment_card(
        row.filename,
        row.kind_label + " · " + row.size_label,
        icon=row.icon,
        maw=ATTACHMENT_WIDTH,
        on_click=state.show_attachment(index),
        class_name="ma-attachment-open",
    )


def _sender_line(state: type[MessageDetailState]) -> rx.Component:
    """``Anna Bauer <anna@example.com>`` — the name in ink, the address grey.

    One line and not two: the address is what the name stands for, so it
    trails it rather than hanging under it, and it disappears entirely when
    the sender gave no name and the two would read the same. The date closes
    the line off at the far right, pushed there by the stylesheet rather than
    by a spacer.
    """
    return mn.group(
        mn.text(state.view.sender_name, class_name="ma-reading-sender"),
        rx.cond(
            state.sender_address_line != "",
            mn.text(state.sender_address_line, class_name="ma-reading-address"),
            rx.fragment(),
        ),
        _graph_glyph(
            state,
            ChipKind.ADDRESS.value,
            state.view.sender_address,
            state.view.sender_name,
        ),
        mn.text(state.view.date, class_name="ma-reading-date"),
        gap=6,
        wrap="nowrap",
        align="center",
        style={"minWidth": 0},
    )


AVATAR_SIZE = 40
"""The initials circle beside the header, in pixels.

It is the block's left gutter: the subject, the sender, the recipients and the
chips all start where the circle ends, which is what makes the header read as
one column rather than as four lines that happen to be near each other.
"""


def _chip(state: type[MessageDetailState], chip: MessageChip) -> rx.Component:
    """One thing the message belongs to, pressed to draw it.

    Two spellings of one pill and the difference is only ``on_click``: a topic
    or a tag opens the graph tab, a group has no view to open — see
    :data:`~mailarc_ui.message_detail.model.DRAWABLE_CHIPS`.
    """
    return rx.cond(
        chip.drawable,
        link_chip(
            chip.icon,
            chip.label,
            on_click=state.show_chip_graph(chip.kind, chip.chip_id, chip.label),
        ),
        link_chip(chip.icon, chip.label),
    )


def message_header(state: type[MessageDetailState]) -> rx.Component:
    """Who wrote it, when, about what, what came with it, and where it belongs.

    The block a mail client draws, in its order and in one column: the subject
    on top as the largest thing on the pane, the sender under it with the date
    opposite, then ``To:`` and ``Cc:`` set quietly, then the chips naming the
    topic, the tags and the group this mail sits in. The initials circle is the
    gutter beside all of it, which is what aligns the subject with the names
    below rather than leaving it hanging out to the left.
    """
    return mn.group(
        avatar_initials(state.view.sender_name, size=AVATAR_SIZE),
        mn.stack(
            mn.group(
                mn.title(
                    state.view.subject,
                    order=3,
                    line_clamp=2,
                    class_name="ma-reading-subject",
                ),
                _graph_glyph(
                    state,
                    ChipKind.MESSAGE.value,
                    state.selected_id,
                    state.view.subject,
                ),
                gap=6,
                wrap="nowrap",
                align="flex-start",
                style={"minWidth": 0},
            ),
            _sender_line(state),
            _recipient_line(state, "To:", state.view.recipients),
            rx.cond(
                state.has_cc,
                _recipient_line(state, "Cc:", state.view.cc),
                rx.fragment(),
            ),
            rx.cond(
                state.chips,
                mn.group(
                    rx.foreach(state.chips, lambda one: _chip(state, one)),
                    gap=6,
                    wrap="wrap",
                    class_name="ma-reading-chips",
                ),
                rx.fragment(),
            ),
            rx.cond(
                state.has_attachments,
                mn.group(
                    rx.foreach(
                        state.view.attachments,
                        lambda row, index: _attachment_card(state, row, index),
                    ),
                    gap="xs",
                    class_name="ma-reading-files",
                ),
                rx.fragment(),
            ),
            gap=4,
            style={"minWidth": 0, "flex": "1 1 0%"},
        ),
        gap=12,
        align="flex-start",
        wrap="nowrap",
        class_name="ma-reading-header",
    )


def remote_content_bar(state: type[MessageDetailState]) -> rx.Component:
    """The question a mail client asks before it fetches anything remote."""
    return message(
        mn.group(
            mn.text(state.remote_notice, size="sm", flex="1"),
            soft_button(
                "Allow once",
                on_click=state.allow_remote_once,
                size="xs",
            ),
            quiet_button(
                "Allow for this sender",
                on_click=state.allow_remote_for_sender,
                size="xs",
            ),
            gap="sm",
            align="center",
            wrap="wrap",
        ),
        "warning",
        icon="shield",
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
            message(
                "Only the beginning is shown; the full original is on disk.",
                "warning",
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


PREVIEW_FRAME = {
    **GROW,
    "width": "100%",
    "border": "0",
    "background": "var(--ma-surface)",
    "borderRadius": "var(--mantine-radius-sm)",
}
"""The frame an attachment renders in, sized by its parent's layout.

Same flex sizing and same reason as :data:`GROW` — a percentage height inside
a drawer body resolves against nothing and collapses to 150 pixels.
"""


def _preview_head(state: type[MessageDetailState]) -> rx.Component:
    """What the file is, over what it shows: the type and the size.

    The drawer's own title carries the name, so this says the two things a
    name does not — and it says them for a file that has no preview too,
    where they are the whole of the answer.
    """
    return mn.group(
        mn.text(state.preview.content_type, class_name="ma-preview-type"),
        mn.text(state.preview.size_label, class_name="ma-preview-size"),
        gap=8,
        align="baseline",
        wrap="wrap",
    )


def _preview_body(state: type[MessageDetailState]) -> rx.Component:
    """The file itself, in whichever shape it takes.

    Still a chain of ``rx.cond`` over one string and not a lookup, because the
    choice is made in the browser: what crosses is
    :attr:`~mailarc_ui.message_detail.preview.AttachmentPreview.kind` as text,
    and each branch is a different element with different props.

    Written as a table and folded into that chain rather than nested by hand.
    The hand-nested version was four deep when there were four shapes and eight
    when there were eight, by which point the last branch sat forty columns in
    and adding a shape meant re-indenting every one below it. This produces the
    identical component; what it buys is that the shapes are a list somebody
    can read down, with ``NONE`` visibly at the end as the one that catches
    everything else.
    """
    shapes = (
        (PreviewKind.IMAGE, _preview_image),
        (PreviewKind.PDF, _preview_pdf),
        (PreviewKind.HTML, _preview_html),
        (PreviewKind.TEXT, _preview_text),
        (PreviewKind.CALENDAR, _preview_calendar),
        (PreviewKind.CONTACT, _preview_contact),
        (PreviewKind.SLIDES, _preview_slides),
        (PreviewKind.DOCUMENT, _preview_document),
        (PreviewKind.SPREADSHEET, _preview_spreadsheet),
    )
    body = _preview_none(state)
    for kind, build in reversed(shapes):
        body = rx.cond(state.preview.kind == kind.value, build(state), body)
    return body


def _preview_image(state: type[MessageDetailState]) -> rx.Component:
    """A picture, scaled to the panel and centred on it.

    Straight in the page rather than in a frame: an ``<img>`` never runs what
    is inside it — an SVG's script included — so the frame would buy nothing
    and would cost the picture its ability to size itself to the panel.
    """
    return mn.center(
        rx.image(
            src=state.preview.source,
            alt=state.preview.filename,
            style={"maxWidth": "100%", "maxHeight": "100%", "objectFit": "contain"},
        ),
        style={**GROW, "overflow": "auto"},
    )


def _preview_pdf(state: type[MessageDetailState]) -> rx.Component:
    """A PDF, drawn by pdfium in the page rather than by the browser.

    This used to be an ``<iframe>`` pointed at a ``data:`` URI — the one frame
    in the application without a ``sandbox`` attribute, because the browser's
    PDF viewer is a plugin and ``sandbox=""`` blocks plugins. That worked. It
    was replaced because it was the only one of the four document kinds with
    no page rail and no shared header, so an invoice opened after a deck was a
    different viewer.

    The isolation the frame gave is not lost with it: the bytes now go to a
    WebAssembly parser in this page rather than to a plugin, and nothing about
    them is fetched or executed. See :mod:`mailarc_ui.kit.viewer_pdf`.
    """
    return pdf_viewer(
        encoded=state.preview.encoded,
        file_name=state.preview.filename,
    )


def _preview_html(state: type[MessageDetailState]) -> rx.Component:
    """A page of HTML in the same sandbox a mail body gets, under the same CSP."""
    return rx.el.iframe(
        src_doc=state.preview.document,
        sandbox="",
        referrer_policy="no-referrer",
        title=state.preview.filename,
        style=PREVIEW_FRAME,
    )


def _preview_text(state: type[MessageDetailState]) -> rx.Component:
    """A text file, set monospaced and wrapped, scrolling inside the panel."""
    return mn.scroll_area(
        mn.code(
            state.preview.text,
            block=True,
            style={"whiteSpace": "pre-wrap", "wordBreak": "break-word"},
        ),
        type="hover",
        style=GROW,
    )


def _preview_calendar(state: type[MessageDetailState]) -> rx.Component:
    """A meeting, laid out rather than printed — see
    :mod:`mailarc_ui.message_detail.records`."""
    return calendar_preview(state, style=GROW)


def _preview_contact(state: type[MessageDetailState]) -> rx.Component:
    """A contact card, laid out rather than printed."""
    return contact_preview(state, style=GROW)


def _preview_slides(state: type[MessageDetailState]) -> rx.Component:
    """A slide deck, drawn from the bytes rather than described.

    Each of these three renderers is a lazy chunk of about a megabyte, fetched
    only when somebody opens a file of that kind — a reader who never gets a
    deck never pays for one. See :mod:`mailarc_ui.kit.viewer`.
    """
    return pptx_viewer(
        encoded=state.preview.encoded,
        file_name=state.preview.filename,
    )


def _preview_document(state: type[MessageDetailState]) -> rx.Component:
    """A Word document, with a rail of page previews beside it."""
    return docx_viewer(
        encoded=state.preview.encoded,
        file_name=state.preview.filename,
    )


def _preview_spreadsheet(state: type[MessageDetailState]) -> rx.Component:
    """A workbook, read-only, navigated by its own sheet tabs.

    ``is_dark`` is passed because a spreadsheet paints its own grid: left to
    the library's default it would be a white sheet in a dark panel.
    """
    return xlsx_viewer(
        encoded=state.preview.encoded,
        file_name=state.preview.filename,
        is_dark=rx.color_mode == "dark",
    )


def _preview_none(state: type[MessageDetailState]) -> rx.Component:
    """A file a browser cannot draw — said plainly, with its glyph."""
    return empty_panel(
        state.preview.icon,
        "No preview",
        state.preview.filename,
        style=GROW,
    )


def attachment_panel(state: type[MessageDetailState]) -> rx.Component:
    """The slide-in panel one of the header's files opens into.

    Mounted beside the tabs rather than inside the message panel, so that the
    reader who opens a file, looks at the source and comes back finds the same
    panel where they left it — a drawer inside a tab that unmounts would take
    the file with it.
    """
    return slide_over(
        mn.stack(
            _preview_head(state),
            rx.cond(
                state.preview_note_shown,
                message(state.preview.note, "note"),
                rx.fragment(),
            ),
            rx.cond(state.preview_loading, spinner(), _preview_body(state)),
            gap="sm",
            style=COLUMN,
        ),
        opened=state.preview_open,
        on_close=state.close_attachment,
        title=state.preview.filename,
    )


def message_tabs(
    state: type[MessageDetailState],
    graph_panel: rx.Component | None = None,
) -> rx.Component:
    """The right column: Message and Source, or a prompt to pick something.

    ``graph_panel`` is the third tab's body, and it is an argument rather than
    something built here because the reading pane cannot draw a graph: the
    canvas and its projection live in :mod:`mailarc_ui.graph`, which already
    reads this module for its own reading column. Whoever composes the pane
    supplies the panel — :func:`mailarc_ui.graph.chip_graph_panel` — and a
    caller that passes none gets the two tabs it always had.

    The tab itself only exists once a chip has been pressed, which is why it
    is behind an ``rx.cond`` and carries :attr:`chip_label` rather than a
    fixed name: it stands for that topic, tag or group, not for a view.
    """
    return rx.cond(
        state.has_selection,
        rx.cond(
            state.loading_raw,
            spinner(),
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
                    _chip_tab(state, graph_panel),
                ),
                _tab_panel(TAB_MESSAGE, message_view(state)),
                _tab_panel(TAB_SOURCE, raw_message_view(state)),
                _chip_panel(state, graph_panel),
                value=state.tab,
                on_change=state.show_tab,
                keep_mounted=False,
                # `styles` is not a declared prop on the wrapper; through
                # `custom_attrs` it reaches Mantine as the real thing.
                custom_attrs={"styles": TABS_STYLES},
            ),
        ),
        empty_panel(
            "mail-open",
            "Pick a message",
            "It shows up here, readable or as its raw source.",
        ),
    )


def message_pane(
    state: type[MessageDetailState],
    graph_panel: rx.Component | None = None,
) -> rx.Component:
    """The tabs, and the panel a file on the open message slides in over.

    One function so that a page composing a reading pane gets both without
    having to know the second one exists — and so the drawer sits outside the
    tabs, where ``keep_mounted=False`` cannot take it down mid-read.
    """
    return rx.fragment(message_tabs(state, graph_panel), attachment_panel(state))


def _chip_tab(
    state: type[MessageDetailState], graph_panel: rx.Component | None
) -> rx.Component:
    """The third tab, after Source, when there is one to show."""
    if graph_panel is None:
        return rx.fragment()
    return rx.cond(
        state.chip_open,
        mn.tabs.tab(
            state.chip_label,
            value=TAB_GRAPH,
            left_section=rx.icon("share-2", size=14),
        ),
        rx.fragment(),
    )


def _chip_panel(
    state: type[MessageDetailState], graph_panel: rx.Component | None
) -> rx.Component:
    """Its body — mounted only while the tab is on the strip.

    ``keep_mounted=False`` takes care of the panel that is not selected; this
    guards the case Mantine cannot see, which is a panel whose *tab* is gone.
    """
    if graph_panel is None:
        return rx.fragment()
    return rx.cond(state.chip_open, _tab_panel(TAB_GRAPH, graph_panel), rx.fragment())
