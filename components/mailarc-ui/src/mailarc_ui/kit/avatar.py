"""The initials circle a sender gets.

``mn.avatar`` already does the two hard parts — deriving initials from a name
and picking that name a stable colour (``color="initials"``) — so this is one
call with the archive's two sizes baked into a default: 36px in the mail
list, 44 in the reading-pane header.
"""

from __future__ import annotations

import appkit_mantine as mn
import reflex as rx


def avatar_initials(name: rx.Var | str, size: int = 36) -> rx.Component:
    """A sender's initials on their colour, in a circle of ``size`` px."""
    return mn.avatar(
        name=name,
        color="initials",
        size=size,
        class_name="ma-avatar",
    )
