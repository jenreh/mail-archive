"""The panel that slides in from the right over whatever was there.

One shape, one side, one size. A drawer is the gesture for *look at this
without losing your place*: the list, the header and the open mail all stay
where they were, and closing the panel puts the reader back exactly where they
pressed. A modal would take the screen for a thing that is a detail of the
message behind it.

It opens from the right because that is the direction the archive already
reads in — the list on the left, the message beside it, and one step further
right is the file on that message. From the left it would slide over the
navigation rail and read as a change of place rather than a closer look.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx

SLIDE_OVER_SIZE = "min(1120px, 94vw)"
"""How wide the panel is.

Wide enough for an A4 page at a readable scale, which is what most of what
arrives on mail turns out to be. Narrower and a PDF is a column of grey.

Wider than Mantine's ``xl`` since the panel started opening documents rather
than describing them. A Word document and a PDF each carry a rail of page
previews, and ``kit/viewer_shell.jsx`` puts that rail beside the
page only above 768 pixels of *container*: at ``xl`` the panel was about 800
pixels before its own padding, so every rail was an overlay and the reader had
to ask for the thing they had just opened the file to see.

The ``vw`` half is what keeps that honest on a laptop — a fixed 1120 would be
wider than the window, and Mantine would clamp it to 100% with no room left
for the page behind. Below the breakpoint the rail is still reachable; it is
one press rather than nothing.
"""

SLIDE_OVER_STYLES = {
    # A drawer's body is a plain block with padding, so a frame inside it falls
    # back to the browser's 150 pixels — the same trap `message_detail`'s
    # TABS_STYLES documents. Making the body a growing column lets whatever is
    # in it take the height that is left. Written through `styles` because
    # Mantine's own stylesheet beats a class of ours.
    "content": {"display": "flex", "flexDirection": "column"},
    "body": {
        "flex": "1 1 0%",
        "minHeight": 0,
        "display": "flex",
        "flexDirection": "column",
    },
}
"""What makes a drawer's body fill the panel instead of hugging its text."""


def slide_over(
    *children: Any,
    opened: rx.Var | bool,
    on_close: Any,
    title: rx.Var | str = "",
    **props: Any,
) -> rx.Component:
    """A panel over the page from the right, with a title and a way out.

    ``opened`` and ``on_close`` are keyword-only and required, because a
    drawer with neither is a component that can never be closed — the one
    mistake this wrapper exists to make impossible.
    """
    props.setdefault("size", SLIDE_OVER_SIZE)
    props.setdefault("padding", "lg")
    props.setdefault("keep_mounted", False)
    return mn.drawer(
        *children,
        opened=opened,
        on_close=on_close,
        title=title,
        position="right",
        # `styles` is not a declared prop on the wrapper; through `custom_attrs`
        # it reaches Mantine as the real thing rather than as a CSS key.
        custom_attrs={"styles": SLIDE_OVER_STYLES},
        **props,
    )
