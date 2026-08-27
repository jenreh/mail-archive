"""The four pieces every page of this archive is built out of.

Not a component library and not an abstraction over Mantine — four functions
that were already in the repository, written privately and repeatedly, and are
now written once. ``stat_tile`` and ``card_heading`` were private helpers in
``insights/components.py``; ``panel_card`` was a fourteen-times-identical
``mn.card`` call; ``page_header`` was the title-and-subtitle block each page
open-coded.

What earns each one its place is that the design has exactly one of it. Where
the design has one card, one heading and one title block, a second spelling is
not flexibility — it is the drift that made the fourteen copies disagree.

Everything visual here comes from ``assets/css/mail-archive.css`` through a
class name. Nothing in this package names a colour.
"""

from mailarc_ui.kit.card import CardTone, card_heading, panel_card
from mailarc_ui.kit.header import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.kit.stat import stat_tile

__all__ = [
    "PAGE_GAP",
    "PAGE_PADDING",
    "CardTone",
    "card_heading",
    "page_header",
    "panel_card",
    "stat_tile",
]
