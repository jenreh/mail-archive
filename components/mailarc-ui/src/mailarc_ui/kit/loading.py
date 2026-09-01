"""What stands where content will be, while a read is out.

Two of them, and the difference is what the reader is owed.

A **spinner** replaces something whose size nobody can predict — a list of
results, a verdict, a set of counts. ``mn.group(mn.loader(size="sm"),
justify="center", py=…)`` was written five times across four modules for
exactly that, and the padding had already drifted between ``lg`` and ``xl``,
so the same wait was a different height depending on which panel was waiting.

A **placeholder block** replaces something whose size *is* known: a dashboard
chart is always the same height, so a grey block of that height keeps the page
from jumping when the read lands. A spinner there would collapse the card and
push everything below it up, which is a worse thing to do to somebody reading
the row underneath than showing them a rectangle.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx

WAIT_PADDING = "lg"
"""The room a spinner gets. One value, because a wait is a wait."""

BLOCK_HEIGHT = 96
"""What a dashboard panel is worth in grey, before its chart arrives."""


def spinner(**props: Any) -> rx.Component:
    """A small centred spinner, for a wait whose shape is not known yet."""
    props.setdefault("py", WAIT_PADDING)
    return mn.group(mn.loader(size="sm"), justify="center", **props)


def placeholder_block(**props: Any) -> rx.Component:
    """A grey block the size of what is coming, so the page does not jump."""
    props.setdefault("h", BLOCK_HEIGHT)
    return mn.skeleton(radius="md", w="100%", **props)
