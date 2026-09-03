"""The pieces every page of this archive is built out of.

Not a component library and not an abstraction over Mantine — functions that
were already in the repository, written privately and repeatedly, and are now
written once. ``stat_tile`` and ``card_heading`` were private helpers in
``insights/components.py``; ``panel_card`` was a fourteen-times-identical
``mn.card`` call.

The mail-client redesign grew the kit by the vocabulary its screens repeat:
form fields (``inputs``), the three buttons (``buttons``), the row chips
(``chips``), the selectable list row (``listrow``), the attachment card
(``attachment``), the sender avatar (``avatar``) and the reader column with an
edge of its own (``layout``).

What earns each one its place is that the design has exactly one of it. Where
the design has one card, one field and one chip, a second spelling is not
flexibility — it is the drift that made the fourteen copies disagree.

Everything visual here comes from ``assets/css/mail-archive.css`` through a
class name, with radius and size defaults from ``mailarc_ui/theme.py``.
Nothing in this package names a colour.
"""

from mailarc_ui.kit.attachment import attachment_card
from mailarc_ui.kit.avatar import avatar_initials
from mailarc_ui.kit.badge import dot_badge, status_badge
from mailarc_ui.kit.buttons import (
    pill_action,
    primary_button,
    quiet_button,
    soft_button,
)
from mailarc_ui.kit.card import CardTone, card_heading, panel_card
from mailarc_ui.kit.chips import count_chip, label_chip, relevance_chip
from mailarc_ui.kit.empty import empty_panel
from mailarc_ui.kit.graph import graph_canvas
from mailarc_ui.kit.message import MessageTone, message, toned_message
from mailarc_ui.kit.layout import (
    COLUMN_GAP,
    LIST_WIDTH,
    PAGE_GAP,
    PAGE_INSET,
    PAGE_PADDING,
    column_card,
)
from mailarc_ui.kit.inputs import (
    FIELD_GAP,
    LABEL_GAP,
    date_field,
    field_label,
    field_note,
    input_field,
    number_field,
    password_field,
    range_switch,
    segmented_field,
    select_field,
)
from mailarc_ui.kit.listrow import list_row
from mailarc_ui.kit.loading import placeholder_block, spinner
from mailarc_ui.kit.progress import job_progress, meter_bar, score_bar
from mailarc_ui.kit.stat import stat_tile
from mailarc_ui.kit.table import VISIBLE_ROWS, scroll_table
from mailarc_ui.kit.validation import REQUIRED, FieldErrors

__all__ = [
    "COLUMN_GAP",
    "FIELD_GAP",
    "LABEL_GAP",
    "LIST_WIDTH",
    "PAGE_GAP",
    "PAGE_INSET",
    "PAGE_PADDING",
    "REQUIRED",
    "VISIBLE_ROWS",
    "CardTone",
    "FieldErrors",
    "MessageTone",
    "attachment_card",
    "avatar_initials",
    "card_heading",
    "column_card",
    "count_chip",
    "date_field",
    "dot_badge",
    "empty_panel",
    "field_label",
    "field_note",
    "graph_canvas",
    "input_field",
    "job_progress",
    "label_chip",
    "list_row",
    "message",
    "meter_bar",
    "number_field",
    "panel_card",
    "password_field",
    "pill_action",
    "placeholder_block",
    "primary_button",
    "quiet_button",
    "range_switch",
    "relevance_chip",
    "score_bar",
    "scroll_table",
    "segmented_field",
    "select_field",
    "soft_button",
    "spinner",
    "stat_tile",
    "status_badge",
    "toned_message",
]
