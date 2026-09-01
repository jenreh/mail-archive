"""A job running, and a measurement standing still — the two bars this design has.

The job row is the interesting one. Imports, the embedder and the insights
rebuild all draw the same thing: a badge saying where the job stands, a bar,
the percentage, and the counts behind it. Written three times, and by the time
this module was made they had already disagreed in two ways that a reader
would meet rather than a reviewer: the insights row had lost ``.ma-tabular``
from both of its numbers, so its digits jittered as the job ran, and the
imports row had no status badge at all, so the one panel where a job is
started was the one that would not say the job had failed.

A percentage never stands alone here. It says how far along, and the counts
beside it say how far along *what* — a bar at 100% over four messages and a
bar at 100% over forty thousand are not the same news.
"""

from __future__ import annotations

from typing import Any

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit.badge import status_badge

PERCENT_WIDTH = 52
"""Wide enough for ``100%``, so the counts beside it do not shift as it climbs."""


def job_progress(
    percent: rx.Var | float,
    percent_label: rx.Var | str,
    detail_label: rx.Var | str,
    active: rx.Var | bool,
    *,
    status: rx.Var | str = "",
    status_color: rx.Var | str = "gray",
) -> rx.Component:
    """One running job, as all three panels draw it.

    ``status`` is optional only because the search for a caller without one is
    what found the imports panel missing its badge; a panel that has a status
    should pass it.
    """
    return mn.group(
        _status(status, status_color),
        mn.progress(
            value=percent,
            color="blue",
            size="lg",
            striped=active,
            animated=active,
            flex="1",
        ),
        mn.text(
            percent_label,
            size="sm",
            fw=600,
            w=PERCENT_WIDTH,
            ta="right",
            class_name="ma-tabular",
        ),
        mn.text(detail_label, size="sm", c="dimmed", class_name="ma-tabular"),
        gap="sm",
        align="center",
        w="100%",
    )


def _status(status: rx.Var | str, color: rx.Var | str) -> rx.Component:
    """Where the job stands, and nothing at all for a panel that has no status.

    The same shape as ``kit.inputs._hint``: a ``Var`` is decided in the
    browser, a literal here. Deciding a literal here rather than handing
    ``rx.cond`` a constant is what keeps a panel that never has a status from
    shipping a badge in a branch that can never be taken.
    """
    if isinstance(status, str):
        return status_badge(status, color) if status else rx.fragment()
    return rx.cond(status != "", status_badge(status, color), rx.fragment())


def meter_bar(
    percent: rx.Var | float,
    color: rx.Var | str,
    **props: Any,
) -> rx.Component:
    """The thin bar under a dashboard measurement.

    Not a job: nothing is running, so it is never striped-and-animated, and it
    is drawn at a fraction of the job bar's weight because a standing figure
    should not read as work in progress.
    """
    props.setdefault("class_name", "ma-meter")
    return mn.progress(
        value=percent,
        color=color,
        size=8,
        radius="xl",
        striped=True,
        animated=False,
        **props,
    )


def score_bar(percent: rx.Var | float, color: rx.Var | str = "grape") -> rx.Component:
    """A score inside a table cell, at the width a column can spare."""
    return mn.progress(value=percent, color=color, size="sm", w=64)
