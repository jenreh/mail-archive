"""The disclosure a conversation's heading wears.

A grouped mail list has two kinds of line and only one of them is new. A member
row is :func:`~mailarc_ui.kit.listrow.list_row` exactly as it always was; a
heading is that same row with a chevron in front of it and the group's size
beside it, so a collapsed conversation still reads as the message it is showing
rather than as a piece of chrome over one.

That is what makes the two gestures separable: the row underneath the chevron
goes on meaning "open this message", and the chevron alone means "show me the
rest of the conversation". Every mail client that groups behaves this way, and
a heading built as its own kind of thing would have had to invent a third.

The open state travels as ``data-expanded`` rather than as an ``rx.cond`` over
a transform, for the reason :mod:`~mailarc_ui.kit.listrow` records for
``data-selected``: a value that crosses the Var boundary as a string cannot
also carry a rotation, and the stylesheet can key on the attribute.

:func:`group_header` is the third kind of line, for a group that is *not* a
conversation — the mail one sender wrote, one topic holds, one tag was put on.
No message can stand in for such a group, so it gets a line of its own: the
chevron, the group's name and how many of it the list is showing. Nothing
opens behind it; the whole line is the one gesture.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit.chips import count_chip

CHEVRON = "chevron-down"
"""Pointing down when the group is open; the stylesheet turns it when it is not."""

CHEVRON_SIZE = 16

SECTION_ICON = "layers"
"""What a section's size chip wears — a stack, for a group of anything."""


def group_chevron(
    *,
    expanded: rx.Var | bool = True,
    on_click: Any = None,
    **props: Any,
) -> rx.Component:
    """The triangle that opens and closes one conversation.

    Its own hit area rather than a corner of the row, because the two gestures
    it sits between mean different things: the row opens a message, this opens
    a group. ``on_click`` is expected to stop the event reaching the row.
    """
    attrs = dict(props.pop("custom_attrs", None) or {})
    attrs["data-expanded"] = rx.cond(expanded, "true", "false")
    return mn.box(
        rx.icon(CHEVRON, size=CHEVRON_SIZE),
        class_name="ma-group-chevron",
        custom_attrs=attrs,
        on_click=on_click,
        **props,
    )


def group_header(
    label: rx.Var | str,
    count: rx.Var | int | str,
    *,
    expanded: rx.Var | bool = True,
    on_click: Any = None,
    **props: Any,
) -> rx.Component:
    """The line over a group that no message can stand in for.

    The chevron inside it carries no handler: the line is one gesture, and a
    chevron that opened the group while the line beside it also opened the
    group would toggle it twice. ``data-expanded`` is on the line as well as
    on the chevron, so the stylesheet can key a closed section's look on it.
    """
    attrs = dict(props.pop("custom_attrs", None) or {})
    attrs["data-expanded"] = rx.cond(expanded, "true", "false")
    return mn.group(
        group_chevron(expanded=expanded),
        mn.text(
            label,
            size="sm",
            fw=600,
            truncate="end",
            style={"minWidth": 0, "flex": "1 1 auto"},
        ),
        count_chip(SECTION_ICON, count),
        class_name="ma-group-header",
        custom_attrs=attrs,
        on_click=on_click,
        gap=8,
        align="center",
        wrap="nowrap",
        w="100%",
        **props,
    )
