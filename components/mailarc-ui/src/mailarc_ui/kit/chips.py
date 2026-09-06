"""The small pills a mail row wears under its preview.

Four variants because the list has three things to say about a message: how
many attachments it carries (``count_chip``), which labels it holds
(``label_chip``), and — in search mode — how relevant it is
(``relevance_chip``). The fourth is the reading pane's: ``link_chip``, a pill
that *leads somewhere* — the topic, the tag or the group the open message
belongs to, pressed to draw it. All of them are the same 8px-radius pill from
``assets/css/mail-archive.css``; the variant classes only recolour it.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx


def count_chip(icon: str, count: rx.Var | int | str) -> rx.Component:
    """A glyph and its count — the paperclip-and-2 on a row with attachments."""
    return mn.group(
        rx.icon(icon, size=12),
        mn.text(count, class_name="ma-chip-text ma-tabular"),
        gap=4,
        align="center",
        wrap="nowrap",
        class_name="ma-chip-pill ma-chip-count",
    )


def label_chip(text: rx.Var | str, color: rx.Var | str = "gray.6") -> rx.Component:
    """A label's name behind its coloured dot.

    ``color`` is a Mantine palette key (``"teal.5"``) or a Var producing one —
    the dot is the one place a label's own colour reaches the row, and it
    crosses as a name, never as a hex value.
    """
    return mn.group(
        mn.box(class_name="ma-chip-dot", bg=color),
        mn.text(text, class_name="ma-chip-text"),
        gap=6,
        align="center",
        wrap="nowrap",
        class_name="ma-chip-pill ma-chip-label",
    )


def relevance_chip(label: rx.Var | str) -> rx.Component:
    """The accent-tinted score a search hit carries — ``92%``."""
    return mn.group(
        mn.text(label, class_name="ma-chip-text ma-tabular"),
        gap=4,
        align="center",
        wrap="nowrap",
        class_name="ma-chip-pill ma-chip-relevance",
    )


def link_chip(icon: str, text: rx.Var | str, **props: Any) -> rx.Component:
    """A glyph and a name on a pill that leads somewhere — a topic, a tag.

    Pressable when the caller passes ``on_click`` and flat when it does not,
    and that is the whole variant: a chip naming something the archive cannot
    draw must not look pressable, because a pill that highlights under the
    pointer and then does nothing is worse than one that never invited the
    click. Nothing here decides *which* of the two a chip is — the caller
    knows whether it has somewhere to go.
    """
    return mn.group(
        rx.icon(icon, size=12),
        mn.text(text, class_name="ma-chip-text"),
        gap=5,
        align="center",
        wrap="nowrap",
        class_name=(
            "ma-chip-pill ma-chip-link"
            if "on_click" in props
            else "ma-chip-pill ma-chip-inert"
        ),
        **props,
    )
