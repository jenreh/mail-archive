"""One number with its name under it."""

import appkit_mantine as mn
import reflex as rx


def stat_tile(
    label: str,
    value: rx.Var | int | str,
    color: rx.Var | str = "inherit",
) -> rx.Component:
    """A figure over its label.

    ``color`` is a Mantine colour name or a Var producing one, because the one
    thing a caller ever changes about a number here is whether it is alarming:
    ``unidentified`` turns red when it is not zero, and nothing else in the
    grid moves.

    Tabular numerals, like every other number in this interface. A column of
    counts that re-renders should change its digits, not its width.
    """
    return mn.stack(
        mn.text(value, fw=700, fz=26, c=color, class_name="ma-tabular"),
        mn.text(label, size="xs", c="dimmed"),
        gap=0,
        style={"minWidth": 0},
    )
