"""What a page is made of, before anything on it: spacing, and a column.

This module used to hold ``page_header`` as well — the title-and-subtitle block
every page opened with. It is gone deliberately. The reference design carries
no page chrome at all: the rail says where you are, and the content starts at
the top edge of the window, which is what gives a three-column reader its whole
height instead of the height left over after a heading.

What remains is the geometry every page shares, named once so that two pages
cannot drift apart by four pixels.
"""

from typing import Any

import reflex as rx

from mailarc_ui.kit.card import panel_card

PAGE_PADDING = "24px"
"""The margin between the window and anything on it."""

RAIL_GAP = "3px"
"""The margin on the side the icon rail is on — half of :data:`PAGE_PADDING`.

The rail already carries sixteen pixels of its own between an icon and its
right edge, so a full page margin beside it reads as forty: the content column
drifts away from the navigation it belongs to, and on a three-column reader
that is forty pixels the message could have had. Twelve is the gutter; the
rail's own inset does the rest of the separating.
"""

PAGE_INSET = f"{PAGE_PADDING} {PAGE_PADDING} {PAGE_PADDING} {RAIL_GAP}"
"""What a page passes as its padding: :data:`PAGE_PADDING` but for the rail side.

One shorthand rather than ``p=PAGE_PADDING, pl=RAIL_GAP``, and the reason is
Mantine's: both style props land as keys of one inline-style object, so which
of ``padding`` and ``paddingLeft`` wins depends on the order Reflex emitted the
props in — and ``Component.get_props()`` returns a set. A single value cannot
lose that race. Mantine's spacing resolver splits on the space and converts
each part, so this arrives as a four-value ``padding`` in rem.
"""

PAGE_GAP = "24px"
"""Between the stacked blocks of a page — cards, panels, alerts."""

COLUMN_GAP = "16px"
"""Between the columns of a reader.

Tighter than :data:`PAGE_GAP` on purpose. The columns of one reader are one
object seen in three parts, and spacing them like unrelated cards reads as
three panels that happen to sit side by side.
"""

LIST_WIDTH = 360
"""The list column, in pixels — the same one on every screen that has one.

Search and ``/admin/accounts`` both open with a list on the left and the thing
it selects beside it, and each carried a ``LIST_WIDTH = 360`` of its own before
this one. Wide enough for a sender and a relative time, which is the widest
line either list carries.

The number is here for the reason the module exists: a reader who moves
between these screens sees the same column in the same place, and two copies
of a literal are how that stops being true.
"""


def column_card(*children: Any, **props: Any) -> rx.Component:
    """One column of a reader, with an edge of its own.

    The reference design draws each column as a separate surface — its own
    border, its own radius, canvas visible between them — rather than as one
    frame divided by hairlines. The difference matters at the corners: a
    divided frame has square internal corners and one outer radius, so the
    middle column reads as a gutter rather than as a thing.

    :func:`~mailarc_ui.kit.panel_card` with the padding taken off, because a
    column pads its own strips and rows at their edges — a card's padding
    would cut every divider and every selected band short of the border. The
    ``ma-column`` class is what actually removes it: ``.ma-card.ma-card``
    states the padding with a doubled class to beat Mantine's own, so
    ``padding=0`` alone loses to the stylesheet. ``overflow: hidden`` is what
    keeps the children inside the radius.
    """
    style = {"overflow": "hidden", **(props.pop("style", None) or {})}
    extra = str(props.pop("class_name", "") or "")
    padding = props.pop("padding", 0)
    if not padding:
        # Flush only when the caller wants it flush. A column holding a list
        # pads nothing — the rows run to the border — while one holding a
        # message keeps the card's own padding, and the class is what decides
        # because the stylesheet outranks the prop either way.
        extra = f"ma-column {extra}".strip()
    return panel_card(
        *children,
        **{
            "padding": padding,
            "h": "100%",
            **props,
            "style": style,
            "class_name": extra,
        },
    )
