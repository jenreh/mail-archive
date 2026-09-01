"""The small pills a mail row wears under its preview.

Three variants because the list has three things to say about a message: how
many attachments it carries (``count_chip``), which labels it holds
(``label_chip``), and — in search mode — how relevant it is
(``relevance_chip``). All of them are the same 8px-radius pill from
``assets/css/mail-archive.css``; the variant classes only recolour it.
"""

from __future__ import annotations

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
