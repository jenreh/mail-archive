"""The pieces every page of this archive is built out of.

Not a component library and not an abstraction over Mantine — functions that
were already in the repository, written privately and repeatedly, and are now
written once. ``stat_tile`` and ``card_heading`` were private helpers in
``insights/components.py``; ``panel_card`` was a fourteen-times-identical
``mn.card`` call; ``page_header`` was the title-and-subtitle block each page
open-coded.

The mail-client redesign grew the kit by the vocabulary its screens repeat:
form fields (``inputs``), the three buttons (``buttons``), the row chips
(``chips``), the selectable list row (``listrow``), the attachment card
(``attachment``) and the sender avatar (``avatar``).

What earns each one its place is that the design has exactly one of it. Where
the design has one card, one field and one chip, a second spelling is not
flexibility — it is the drift that made the fourteen copies disagree.

Everything visual here comes from ``assets/css/mail-archive.css`` through a
class name, with radius and size defaults from ``mailarc_ui/theme.py``.
Nothing in this package names a colour.
"""

from mailarc_ui.kit.attachment import attachment_card
from mailarc_ui.kit.avatar import avatar_initials
from mailarc_ui.kit.buttons import pill_action, primary_button, quiet_button
from mailarc_ui.kit.card import CardTone, card_heading, panel_card
from mailarc_ui.kit.chips import count_chip, label_chip, relevance_chip
from mailarc_ui.kit.header import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.kit.inputs import (
    date_field,
    field_label,
    input_field,
    segmented_field,
    select_field,
)
from mailarc_ui.kit.listrow import list_row
from mailarc_ui.kit.stat import stat_tile

__all__ = [
    "PAGE_GAP",
    "PAGE_PADDING",
    "CardTone",
    "attachment_card",
    "avatar_initials",
    "card_heading",
    "count_chip",
    "date_field",
    "field_label",
    "input_field",
    "label_chip",
    "list_row",
    "page_header",
    "panel_card",
    "pill_action",
    "primary_button",
    "quiet_button",
    "relevance_chip",
    "segmented_field",
    "select_field",
    "stat_tile",
]
