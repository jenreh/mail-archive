"""The initials circle a sender gets.

``mn.avatar`` already does the two hard parts — deriving initials from a name
and picking that name a stable colour (``color="initials"``) — so this is one
call with the archive's two sizes baked into a default: 36px in the mail
list, 44 in the reading-pane header.
"""

from __future__ import annotations

import appkit_mantine as mn
import reflex as rx


def avatar_initials(
    name: rx.Var | str, size: int = 36, src: rx.Var | str | None = None
) -> rx.Component:
    """A sender's initials on their colour, in a circle of ``size`` px.

    ``src`` is for the one place that has a picture of the person: a contact
    card carries its own photograph. Passing it here rather than building a
    second avatar keeps one circle in the application — a photograph and a
    monogram in two different shapes would read as two different kinds of
    person.

    ``None`` and ``""`` are not the same thing and the difference is a request.
    Left out, the element carries no ``src`` at all and draws the initials.
    Given an empty string, it carries ``src=""``, which a browser resolves
    against the page and *fetches* before falling back — a request to our own
    URL for every avatar in a list of results. So a caller with nothing to show
    passes nothing.
    """
    if src is None:
        return mn.avatar(name=name, color="initials", size=size, class_name="ma-avatar")
    return mn.avatar(
        name=name,
        src=src,
        color="initials",
        size=size,
        class_name="ma-avatar",
    )
