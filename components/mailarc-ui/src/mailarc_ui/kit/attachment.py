"""The card an attachment is offered on.

A bordered white rectangle with a file-type glyph in a tinted square, the
filename, and a muted meta line — ``123 KB · Download``. ``mn.paper`` and not
``mn.card``, because ``kit/card.py`` is the one module allowed to build a
card and this is a smaller surface than the one it owns.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx


def attachment_card(
    filename: rx.Var | str,
    meta: rx.Var | str,
    icon: str = "file-text",
    **props: Any,
) -> rx.Component:
    """One attachment: glyph, name, and what clicking it costs.

    ``props`` passes through — ``on_click`` for the download above all — and
    a caller's ``class_name`` is added to ours instead of replacing it.
    """
    extra = str(props.pop("class_name", "") or "")
    return mn.paper(
        mn.group(
            mn.center(
                rx.icon(icon, size=18),
                class_name="ma-attachment-icon",
            ),
            mn.stack(
                mn.text(filename, class_name="ma-attachment-name"),
                mn.text(meta, class_name="ma-attachment-meta"),
                gap=2,
                style={"minWidth": 0},
            ),
            gap=12,
            align="center",
            wrap="nowrap",
        ),
        class_name=f"ma-attachment-card {extra}".strip(),
        **props,
    )
