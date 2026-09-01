"""A listing that shows a window of rows and scrolls the rest under its header.

Every table on the insights page is a ranking — the strongest pairs, the
biggest topics, the best template candidates — and a ranking is read from the
top. Printed in full they turned the page into a column of scrolling that
buried the panel underneath, and the columns' meaning went off screen with the
header row as soon as a reader moved down one.

So the table keeps its own height and pins its head. ``sticky_header`` is
Mantine's own — it positions the ``thead`` against the nearest scrolling
ancestor, which is the box this adds — rather than a copy of it here, and the
height is stated in rows because that is the unit a reader sees: twelve of
them, over the row and header heights the stylesheet names.

Not ``mn.table.scroll_container``. That wrapper declares no props in
``appkit_mantine``, so ``max_height`` on one would be filed as a CSS key on
the div instead of reaching Mantine's ``maxHeight`` — the same trap
``mn.scroll_area`` sets — and it exists for *horizontal* overflow, which is
not what a ranking of twelve needs.
"""

from typing import Any

import appkit_mantine as mn
import reflex as rx

VISIBLE_ROWS = 12
"""How many rows a listing shows before it starts scrolling.

Twelve because that is what fits under a card heading without the panel below
it dropping off a laptop screen, and because a ranking a reader has to scroll
to see the top of is a ranking that lost its point.
"""


def scroll_table(
    *children: Any, rows: int = VISIBLE_ROWS, **props: Any
) -> rx.Component:
    """One listing: ``rows`` visible, the header pinned, the rest scrolled.

    Everything a table on this page always was — striped, hover-highlighted,
    tabular numerals — is applied here rather than at seven call sites, which
    is how the copies would otherwise drift. ``props`` still overrides it.

    The height is inline because it is the one thing that varies per call; the
    scrolling, the pinned head's ground and the hairline under it are in
    ``assets/css/mail-archive.css`` under ``.ma-table-scroll``.
    """
    recipe: dict[str, Any] = {
        "striped": True,
        "highlight_on_hover": True,
        "tabular_nums": True,
        "sticky_header": True,
    }
    return rx.box(
        mn.table(*children, **{**recipe, **props}),
        class_name="ma-table-scroll",
        style={
            "maxHeight": (f"calc({rows} * var(--ma-table-row) + var(--ma-table-head))"),
        },
    )
