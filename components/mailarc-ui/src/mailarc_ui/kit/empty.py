"""Where a list would be, when there is nothing in it yet.

Eight call sites, seven of them the same four props word for word. The eighth —
the insights page — hangs a button under the sentence, which is the one real
variation and the reason ``actions`` exists rather than a second function.

The glyph size is the thing worth stating once. At 28 it reads as a mark on an
empty surface; the moment one page picks 24 and another 32, an empty archive
looks like a different application depending on which page found it empty.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx

ICON_SIZE = 28
"""The mark over an empty list."""


def empty_panel(
    icon: str,
    title: str,
    description: rx.Var | str,
    *actions: Any,
    **props: Any,
) -> rx.Component:
    """A glyph, what is missing, and what to do about it.

    ``description`` takes a ``Var`` because the search page says a different
    sentence depending on whether nothing matched or nothing is archived yet,
    and both are the state's to decide.
    """
    props.setdefault("align", "center")
    return mn.empty_state(
        *((mn.empty_state.actions(*actions),) if actions else ()),
        icon=rx.icon(icon, size=ICON_SIZE),
        title=title,
        description=description,
        **props,
    )
