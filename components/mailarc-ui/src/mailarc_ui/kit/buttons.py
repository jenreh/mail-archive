"""The three buttons this design has.

A primary action is coral and filled; everything beside one is quiet; and the
reading-pane header carries small bordered pills. Three functions rather than
three prop spellings, for the same reason ``panel_card`` exists: the moment a
second page writes its own ``mn.button(variant=…, color=…)`` the two drift.

Colour comes from ``assets/css/mail-archive.css`` through the class each
button wears; radius and size come from the theme.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx


def _merged(base: str, props: dict[str, Any]) -> str:
    """Add the caller's class to ours instead of replacing it."""
    extra = str(props.pop("class_name", "") or "")
    return f"{base} {extra}".strip()


def primary_button(*children: Any, **props: Any) -> rx.Component:
    """The one accent-filled button an action area gets."""
    return mn.button(
        *children,
        variant="filled",
        class_name=_merged("ma-btn-primary", props),
        **props,
    )


def quiet_button(*children: Any, **props: Any) -> rx.Component:
    """The ink-on-nothing button beside a primary one — reset, cancel."""
    return mn.button(
        *children,
        variant="subtle",
        class_name=_merged("ma-btn-quiet", props),
        **props,
    )


def pill_action(label: str, icon: str | None = None, **props: Any) -> rx.Component:
    """A small bordered pill for the reading-pane header's actions."""
    if icon is not None:
        props.setdefault("left_section", rx.icon(icon, size=14))
    return mn.button(
        label,
        variant="default",
        size="xs",
        radius="xl",
        class_name=_merged("ma-pill-action", props),
        **props,
    )
