"""The archive's form fields — one look, stated once.

Every field on every form in this application is the same recipe: a
mono-uppercase label with an optional right-aligned hint, and under it — at a
fixed 8px — the control itself, ``variant="filled"`` wearing the ``.ma-field``
class. The fill, the hairline border and the accent focus ring all live in
``assets/css/mail-archive.css`` under that class; radius and size come from the
Mantine theme (``mailarc_ui/theme.py``) and are deliberately not re-passed
here.

A long explanation goes in ``description`` and lands *under* the control as one
quiet line, which is where the search form has always put its notes. Mantine's
own ``description`` prop would draw a second, differently-styled caption
between the label and the box — two spellings of the same sentence, which is
the drift these functions exist to prevent.

**A field says what is wrong with it through Mantine's own ``error``**, and
that is deliberate rather than incidental. Every one of these controls already
knows how to be invalid: it takes a red border, sets ``aria-invalid`` on the
input a screen reader is on, and prints the message under itself. A form that
reported the same fact as a red alert over the top of it would be spending a
mechanism it already has and moving the message away from the box a person has
to fix — so ``error`` is passed straight through, and the stylesheet only
teaches ``.ma-field`` to wear the state in this archive's palette. See
:mod:`mailarc_ui.kit.validation` for the state half of this.

Ported from voyager's ``voyager_commons/components/inputs.py``, which is the
same idea for a different palette.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import appkit_mantine as mn
import reflex as rx

_FIELD_CLASS = "ma-field"

LABEL_GAP = 8
"""How far the control sits under its label — and a note under its control."""

FIELD_GAP = 18
"""What the design puts between one field and the next.

Wider than :data:`LABEL_GAP` on purpose, and the whole reason a form reads as
rows rather than as a column of text: at an equal gap a label would sit as
close to the box above it as to its own.
"""


def field_label(label: rx.Var | str, hint: rx.Var | str = "") -> rx.Component:
    """Mono uppercase field label with an optional right-aligned hint."""
    return mn.group(
        mn.text(label, class_name="ma-field-label"),
        _hint(hint),
        gap=6,
        align="baseline",
        justify="space-between",
        wrap="nowrap",
        w="100%",
    )


def _hint(hint: rx.Var | str) -> rx.Component:
    """The note across from a label, and nothing at all where there is none.

    A ``Var`` is decided in the browser rather than here — which is how a
    generated field marks itself required, since the provider that declared it
    is what says whether it is.
    """
    drawn = mn.text(hint, class_name="ma-field-hint")
    if isinstance(hint, rx.Var):
        return rx.cond(hint != "", drawn, rx.fragment())
    return drawn if hint else rx.fragment()


def field_note(text: rx.Var | str) -> rx.Component:
    """One quiet line under a control — what a field cannot say in its label."""
    return mn.text(text, size="xs", c="dimmed")


def _field_class(props: dict[str, Any]) -> str:
    """Merge the ``.ma-field`` hook with any caller-supplied class."""
    extra = str(props.pop("class_name", "") or "")
    return f"{_FIELD_CLASS} {extra}".strip()


def _labeled(
    label: rx.Var | str | None,
    hint: rx.Var | str,
    description: str,
    field: rx.Component,
) -> rx.Component:
    """Stack a label over the control and a note under it, or neither."""
    if label is None and not description:
        return field
    parts: list[rx.Component] = []
    if label is not None:
        parts.append(field_label(label, hint))
    parts.append(field)
    if description:
        parts.append(field_note(description))
    return mn.stack(*parts, gap=LABEL_GAP)


def _field(
    control: Callable[..., rx.Component],
    label: rx.Var | str | None,
    hint: rx.Var | str,
    description: str,
    props: dict[str, Any],
) -> rx.Component:
    """One control in the archive's recipe: label over it, note under it."""
    class_name = _field_class(props)
    return _labeled(label, hint, description, control(class_name=class_name, **props))


def input_field(
    label: rx.Var | str | None = None,
    hint: rx.Var | str = "",
    description: str = "",
    **props: Any,
) -> rx.Component:
    """A text input (filled + ``.ma-field``)."""
    props.setdefault("variant", "filled")
    return _field(mn.text_input, label, hint, description, props)


def password_field(
    label: rx.Var | str | None = None,
    hint: rx.Var | str = "",
    description: str = "",
    **props: Any,
) -> rx.Component:
    """A masked input (filled + ``.ma-field``), for a secret being entered."""
    props.setdefault("variant", "filled")
    return _field(mn.password_input, label, hint, description, props)


def number_field(
    label: rx.Var | str | None = None,
    hint: rx.Var | str = "",
    description: str = "",
    **props: Any,
) -> rx.Component:
    """A number input (filled + ``.ma-field``).

    Its ``on_change`` hands over a ``float | str`` rather than an event — an
    emptied box arrives as ``""`` — so a handler bound here has to take both.
    """
    props.setdefault("variant", "filled")
    return _field(mn.number_input, label, hint, description, props)


def select_field(
    label: rx.Var | str | None = None,
    hint: rx.Var | str = "",
    description: str = "",
    **props: Any,
) -> rx.Component:
    """A dropdown select (filled + ``.ma-field``)."""
    props.setdefault("variant", "filled")
    return _field(mn.select, label, hint, description, props)


def date_field(
    label: rx.Var | str | None = None,
    hint: rx.Var | str = "",
    description: str = "",
    **props: Any,
) -> rx.Component:
    """A date input (filled + ``.ma-field``)."""
    props.setdefault("variant", "filled")
    return _field(mn.date_input, label, hint, description, props)


def segmented_field(
    label: rx.Var | str | None = None,
    hint: rx.Var | str = "",
    description: str = "",
    **props: Any,
) -> rx.Component:
    """A segmented control (full-width, ``.ma-field``).

    No ``variant`` — SegmentedControl has none; the light-gray track and the
    white active segment come from the theme and the ``.ma-field`` rules.
    """
    props.setdefault("full_width", True)
    return _field(mn.segmented_control, label, hint, description, props)


def range_switch(**props: Any) -> rx.Component:
    """The switch over a chart — the same control as a field, not the same thing.

    A form field collects a value somebody will save; this changes what a panel
    is showing and saves nothing, so it has no label, no error state and a
    recipe of its own (``.ma-range``): a light pill with a white indicator
    rather than a hairline-bordered track.

    It lives beside :func:`segmented_field` because that is where a reader will
    look for it, and because keeping the two spellings in one file is what
    stops the third one from being written in a page module.
    """
    props.setdefault("size", "xs")
    props.setdefault("radius", "md")
    extra = str(props.pop("class_name", "") or "")
    return mn.segmented_control(
        class_name=f"ma-range {extra}".strip(),
        **props,
    )
