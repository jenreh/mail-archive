"""The card surface, and the heading that sits at the top of one.

One recipe, written fourteen times word for word across ``insights``,
``embedder``, ``accounts``, ``imports`` and the status page before this module
existed. That is not a style question: the copies had already drifted on
``shadow`` and on whether they carried a border, so a card looked slightly different depending on which page you
happened to be on.

The shadow is the one place this deliberately departs from Mantine. ``shadow``
is set to ``"none"`` and the real one arrives from the stylesheet as
``--ma-shadow-card`` — Mantine's own ``shadow="sm"`` is several times heavier
and, against a near-white canvas, makes every card look like it is being
dragged.
"""

from typing import Any, Literal

import appkit_mantine as mn
import reflex as rx

CardTone = Literal["neutral", "warm", "cool"]
"""Which tint a meter row's icon chip wears.

Three because the design has three: an amber chip on the archive-health card, a
blue one on the disk card, and an untinted one anywhere a card has rows but no
colour of its own. A free colour string would put a hex value back into a
component, which is the thing the stylesheet exists to prevent.

It lives here rather than beside the rows because the chip and the card are one
vocabulary, and the card is what a page reaches for first.
"""

_RECIPE: dict[str, Any] = {
    "shadow": "none",
    "padding": "lg",
    "radius": "md",
    "with_border": True,
    "w": "100%",
}
"""What every card in this application is, before anything a caller adds."""


def panel_card(*children: Any, **props: Any) -> rx.Component:
    """One card, with the archive's surface, border and shadow.

    ``props`` overrides the recipe rather than being rejected by it: one call
    site needs ``on_mount`` to prime a search box and another wants tighter
    padding, and a primitive that could not take those would send both back to
    a raw ``mn.card`` — which is how fourteen copies happened the first time. A
    ``class_name`` a caller passes is added to ours instead of replacing it.
    """
    extra = str(props.pop("class_name", "") or "")
    return mn.card(
        *children,
        class_name=f"ma-card {extra}".strip(),
        **{**_RECIPE, **props},
    )


def card_heading(icon: str, title: str) -> rx.Component:
    """A card's glyph and its title, on one line.

    No chip. The tinted square belongs to a *measurement* — a meter row — and
    giving one to the heading as well put a grey box behind every card title
    that the design does not have, and spent the chip's only job.
    """
    return mn.group(
        rx.icon(icon, size=18, class_name="ma-card-icon"),
        mn.text(title, class_name="ma-card-title"),
        gap=10,
        align="center",
        wrap="nowrap",
        class_name="ma-card-heading",
        w="100%",
    )
