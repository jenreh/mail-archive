"""What the archive says in passing, in the four tones it says everything in.

A :func:`~mailarc_ui.kit.message.message` is a remark that stays on the page
until the thing it describes changes. This is the other half of the same
vocabulary: news that is only true for a moment — a button was pressed, the
work it started has ended — and has nowhere on the page to live, because the
control that produced it is a 44px square in a 76px rail.

The tones are :data:`~mailarc_ui.kit.message.MessageTone`'s rather than a
second set, for the reason that module gives: a reader learns red-amber-green-
blue once, and every spelling that disagrees costs them that. What is decided
here is only what an alert does not have to decide — where the thing appears
and how long it stays.

Both of those are decided once for the whole application, which is the reason
this module exists at all instead of three ``rx.toast`` calls at the call site.
Two toasts about the same press arriving in different corners, one of them
gone before it was read, are two different notifications as far as the person
watching is concerned.
"""

from __future__ import annotations

from typing import Any

import reflex as rx
from reflex.event import EventSpec

from mailarc_ui.kit.message import MessageTone

POSITION = "top-right"
"""The corner every toast arrives in.

Not the left: the rail is there, and news about a press would cover the button
that was pressed. The top over the bottom, because the bottom right is where
the reading pane's own content ends up — a strip there lands on the message
somebody is in the middle of.
"""

DURATION_MS = 3000
"""How long one stays.

A second shorter than sonner's default four: one sentence with one number in
it — ``synced 143 messages`` — is read at a glance, and a strip that outstays
that is a strip somebody has to dismiss. The call site can ask for longer, and
:func:`toast` gives every one of them a close button anyway.
"""


LEVELS: dict[MessageTone, str] = {
    "failure": "error",
    "warning": "warning",
    "success": "success",
    "note": "info",
}
"""Each tone's sonner level, which is where its colour and its glyph come from.

The same four keys as the tone table in :mod:`mailarc_ui.kit.message`, and a
table rather than four functions so that "does every tone have one" is a
question a test can ask — a tone added to the alerts and forgotten here fails
with a ``KeyError`` at whichever call site reaches it first, and nowhere else.
"""


def toast(text: str | rx.Var, tone: MessageTone = "note", **props: Any) -> EventSpec:
    """One sentence, in the corner, for as long as it takes to read it.

    Returns an event rather than a component: a toast is something a handler
    *does*, so it is yielded or returned from a state and never placed on a
    page. That is also why nothing here needs the kit's AST guard — there is
    no ``mn.`` element to reach around.
    """
    props.setdefault("position", POSITION)
    props.setdefault("duration", DURATION_MS)
    props.setdefault("close_button", True)
    return rx.toast(text, level=LEVELS[tone], **props)
