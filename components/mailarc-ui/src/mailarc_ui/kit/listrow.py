"""The shell of a selectable list row.

The mail list is rows of very different content over one identical behaviour:
hover shows a soft wash, and the selected row becomes a light-gray rounded
card. The behaviour lives here; the content stays with the list that renders
it.

Selection travels as a ``data-selected`` attribute rather than an ``rx.cond``
over a style dict, for the reason ``.ma-nav-link[data-active]`` already
records: a colour that crosses the Var boundary as a string cannot also carry
a radius and a background, and the stylesheet can key on the attribute.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx


def list_row(
    *children: Any,
    selected: rx.Var | bool = False,
    **props: Any,
) -> rx.Component:
    """One row that knows whether it is the selected one.

    ``selected`` is usually a Var — ``state.selected_id == row.id`` — and the
    row renders ``data-selected="true"`` exactly then, which is what the
    ``.ma-list-row[data-selected="true"]`` rule keys on. Everything else a
    caller passes (``on_click`` above all) goes straight through.
    """
    extra = str(props.pop("class_name", "") or "")
    attrs = dict(props.pop("custom_attrs", None) or {})
    attrs["data-selected"] = rx.cond(selected, "true", "false")
    return mn.box(
        *children,
        class_name=f"ma-list-row {extra}".strip(),
        custom_attrs=attrs,
        **props,
    )
