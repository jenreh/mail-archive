"""The archive's form fields — one look, stated once.

Every field on the search form (and any form after it) is the same recipe:
a mono-uppercase label with an optional right-aligned hint, and under it —
at a fixed 8px — the control itself, ``variant="filled"`` wearing the
``.ma-field`` class. The fill, the hairline border and the accent focus ring
all live in ``assets/css/mail-archive.css`` under that class; radius and size
come from the Mantine theme (``mailarc_ui/theme.py``) and are deliberately
not re-passed here.

Ported from voyager's ``voyager_commons/components/inputs.py``, which is the
same idea for a different palette.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx

_FIELD_CLASS = "ma-field"

_FIELD_GAP = 8
"""How far the control sits under its label."""


def field_label(label: str, hint: str = "") -> rx.Component:
    """Mono uppercase field label with an optional right-aligned hint."""
    return mn.group(
        mn.text(label, class_name="ma-field-label"),
        mn.text(hint, class_name="ma-field-hint") if hint else rx.fragment(),
        gap=6,
        align="baseline",
        justify="space-between",
        wrap="nowrap",
        w="100%",
    )


def _field_class(props: dict[str, Any]) -> str:
    """Merge the ``.ma-field`` hook with any caller-supplied class."""
    extra = str(props.pop("class_name", "") or "")
    return f"{_FIELD_CLASS} {extra}".strip()


def _labeled(label: str | None, hint: str, field: rx.Component) -> rx.Component:
    """Stack a field label above the control, or return the bare control."""
    if label is None:
        return field
    return mn.stack(field_label(label, hint), field, gap=_FIELD_GAP)


def input_field(label: str | None = None, hint: str = "", **props: Any) -> rx.Component:
    """A text input (filled + ``.ma-field``)."""
    field = mn.text_input(variant="filled", class_name=_field_class(props), **props)
    return _labeled(label, hint, field)


def select_field(
    label: str | None = None, hint: str = "", **props: Any
) -> rx.Component:
    """A dropdown select (filled + ``.ma-field``)."""
    field = mn.select(variant="filled", class_name=_field_class(props), **props)
    return _labeled(label, hint, field)


def date_field(label: str | None = None, hint: str = "", **props: Any) -> rx.Component:
    """A date input (filled + ``.ma-field``)."""
    field = mn.date_input(variant="filled", class_name=_field_class(props), **props)
    return _labeled(label, hint, field)


def segmented_field(
    label: str | None = None, hint: str = "", **props: Any
) -> rx.Component:
    """A segmented control (full-width, ``.ma-field``).

    No ``variant`` — SegmentedControl has none; the light-gray track and the
    white active segment come from the theme and the ``.ma-field`` rules.
    """
    props.setdefault("full_width", True)
    field = mn.segmented_control(class_name=_field_class(props), **props)
    return _labeled(label, hint, field)
