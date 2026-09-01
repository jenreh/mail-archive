"""Where a thing stands, in one word.

Eleven call sites and two disagreements worth naming. ``tt="none"`` was passed
at four of them and forgotten at the rest, so the same status read ``idle`` on
the accounts page and ``IDLE`` in the imports table — Mantine uppercases a
badge by default, and these are words the archive chose, not shouting. And the
weight was set once and left at Mantine's bolder default everywhere else.

The colour travels *on* the row rather than being matched here, for the reason
:mod:`mailarc_ui.accounts.state` gives where it keeps the status table:
matching a status in a component would mean switching on a ``Var``.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx


def status_badge(
    text: rx.Var | str,
    color: rx.Var | str = "gray",
    size: str = "sm",
    **props: Any,
) -> rx.Component:
    """What the archive last knew about something, in its own words."""
    props.setdefault("style", {"flexShrink": 0})
    return mn.badge(
        text,
        color=color,
        variant="light",
        size=size,
        tt="none",
        fw=500,
        **props,
    )


def dot_badge(
    text: rx.Var | str,
    color: rx.Var | str = "gray",
    size: str = "lg",
    **props: Any,
) -> rx.Component:
    """A presence, not a status: the dot says on or off, the word says of what.

    Its own function because the two are different claims. A status badge
    reports a value the archive stored; this reports whether a capability is
    there at all, which is why it is a dot beside plain text rather than a
    filled pill.
    """
    return mn.badge(text, color=color, variant="dot", size=size, tt="none", **props)
