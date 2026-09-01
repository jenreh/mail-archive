"""What the archive says back, in the four tones it has to say it in.

``mn.alert`` was open-coded twenty-one times across seven modules, and the
copies had already drifted the way copies do. A confirmation was ``teal`` with
a ``circle-check`` on the accounts page and ``green`` with a ``check`` on the
embedder; a failure carried a title on four pages and none on three; and
``py="xs"`` was passed at some call sites and forgotten at others, so two
alerts saying equally little were different heights.

None of that is a styling preference. A reader learns a colour once — red is a
fault, amber is something to weigh, green is done, blue is context — and every
spelling that disagrees costs them that. So the tone is the argument here and
the colour is not available: a call site says *what kind of thing happened*,
and this module is the only place that decides what that looks like.

``py="xs"`` comes with having no title, which is the honest rule behind the
prop that was being passed by hand: a one-line remark does not need the room a
titled report does.
"""

from __future__ import annotations

from typing import Any, Literal

import appkit_mantine as mn
import reflex as rx

MessageTone = Literal["failure", "warning", "success", "note"]
"""What kind of thing happened — the only thing a call site chooses."""

_TONES: dict[MessageTone, tuple[str, str]] = {
    "failure": ("red", "triangle-alert"),
    "warning": ("yellow", "triangle-alert"),
    "success": ("green", "circle-check"),
    "note": ("blue", "info"),
}
"""Each tone's colour and glyph, stated once for the whole application."""

ICON_SIZE = 16
"""The glyph beside a message, at the size every call site was passing."""


def message(
    text: Any,
    tone: MessageTone = "note",
    title: str = "",
    icon: str = "",
    **props: Any,
) -> rx.Component:
    """One thing the archive has to say, in the tone its kind earns.

    ``text`` takes a component as well as a sentence: the remote-content bar
    is a question with two buttons in it rather than a remark, and it is the
    same yellow bar either way.

    ``icon`` overrides the tone's glyph, and only that. The one caller that
    does is that same bar — a shield, because it is asking about privacy, and
    a warning triangle would say something had gone wrong. The colour stays
    the tone's, which is what keeps the override from becoming a second
    vocabulary.
    """
    color, tone_icon = _TONES[tone]
    return _alert(text, color, icon or tone_icon, title, props)


def toned_message(
    text: Any,
    color: rx.Var | str,
    icon: str = "triangle-alert",
    title: str = "",
    **props: Any,
) -> rx.Component:
    """A message whose tone only the browser knows.

    Two call sites compute their own colour — the embedder's advice and the
    insights page's agreement — and a ``Var`` cannot be looked up in
    :data:`_TONES` here. They still go through this module rather than reaching
    for ``mn.alert``, so the padding, the glyph size and the variant stay one
    decision even where the colour is not.
    """
    return _alert(text, color, icon, title, props)


def _alert(
    text: Any,
    color: rx.Var | str,
    icon: str,
    title: str,
    props: dict[str, Any],
) -> rx.Component:
    """The one ``mn.alert`` this application draws."""
    if title:
        props.setdefault("title", title)
    else:
        props.setdefault("py", "xs")
    return mn.alert(
        text,
        color=color,
        variant="light",
        icon=rx.icon(icon, size=ICON_SIZE),
        **props,
    )
