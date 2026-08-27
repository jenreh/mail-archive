"""The dashboard as it is drawn: one band, six cards, no colour literals.

Everything visual arrives from ``assets/css/mail-archive.css`` through a class
name; nothing in this module names a hex value, and the two places that name a
colour at all name a ``var(--ma-…)`` because Mantine's ``color`` prop takes a
CSS colour and there is no class to put it on.

What the page shows is decided in :mod:`mailarc_ui.dashboard.state`, and one
consequence shows up repeatedly below: **an empty panel is drawn the same way
whether the archive is healthy or the caller was refused.** The notifications
card renders "Nothing needs attention" for an administrator with nothing
pending and for an anonymous visitor alike, and a meter row prints no path when
the state did not put one there. There is no branch here on who is asking,
because there is nothing here to branch on — the state hands over what may be
shown and nothing else.
"""

from typing import Any

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.dashboard.model import MONTH, WEEK, YEAR, MeterView
from mailarc_ui.dashboard.state import DashboardState
from mailarc_ui.kit import CardTone, card_heading, panel_card

RANGE_CHOICES: list[dict[str, str]] = [
    {"label": "Last week", "value": WEEK},
    {"label": "Last month", "value": MONTH},
    {"label": "Last year", "value": YEAR},
]
"""What the switch above the charts offers, in the order it offers it."""

CHART_HEIGHT = 280

_CHART: dict[str, Any] = {
    "data_key": "day",
    "h": CHART_HEIGHT,
    "curve_type": "linear",
    "with_dots": False,
    "with_gradient": True,
    "fill_opacity": 0.12,
    "stroke_width": 2,
    "with_legend": False,
    "with_tooltip": True,
    # "x", not "y". Mantine names the axis the lines run *along*, not the one
    # they cross: measured on the running chart, `grid_axis="y"` produced five
    # vertical lines and no horizontal ones, which is the opposite of the
    # design. The reference draws dotted horizontals only.
    "grid_axis": "x",
    "tick_line": "none",
    "stroke_dasharray": "3 3",
    "y_axis_props": {"width": 44},
    "grid_color": "var(--ma-border)",
    "x_axis_props": {"minTickGap": 12},
}
"""Both charts, to the pixel: horizontal dotted gridlines only, no dots on the
line, no legend, a faint fill under a two-pixel stroke. One dict rather than two
call sites, because the two charts differing by anything except their series
would read as one of them being wrong."""


def dashboard_panel() -> rx.Component:
    """The whole page body: the band, then the grid of cards."""
    return mn.stack(kpi_band(), _band_error(), _cards(), gap="md", w="100%")


def kpi_band() -> rx.Component:
    """Five figures on one gradient, segmented rather than five loose cards.

    A value nobody could read shows the em dash the state put there, never a
    nought — and the "Last Archived" tile is the one most likely to: it comes
    from the graph, along with both charts, and says nothing when the graph is
    down (§1.3).
    """
    return mn.box(
        mn.simple_grid(
            _kpi_tile("mail", "Archived Emails", DashboardState.archived),
            _kpi_tile("clock", "Last Archived", DashboardState.last_archived),
            _kpi_tile("at-sign", "Accounts", DashboardState.accounts),
            _kpi_tile("briefcase", "Emails queue", DashboardState.queued),
            _kpi_tile("users", "Users", DashboardState.users),
            cols={"base": 1, "xs": 2, "sm": 3, "lg": 5},
            spacing=0,
        ),
        class_name="ma-kpi-band",
        w="100%",
    )


def _band_error() -> rx.Component:
    """Why three of the band's tiles are showing an em dash.

    The KPI tiles have no room for a sentence and no honest way to hold one —
    ``—`` is what a tile says when nobody could read its number — so the reason
    goes under the band. Without it a failed SQLite read left three tiles
    dashed with nothing on the page saying why, which is the state a reader
    cannot tell apart from an archive that has no accounts and no users.

    Nothing while the read is still out: an alert that appeared and vanished on
    every page load would be a fault the page reports about itself.
    """
    return rx.cond(
        DashboardState.loading_counts,
        rx.fragment(),
        rx.cond(
            DashboardState.counts_error != "",
            mn.alert(
                DashboardState.counts_error,
                color="red",
                variant="light",
                icon=rx.icon("triangle-alert", size=16),
                py="xs",
                w="100%",
            ),
            rx.fragment(),
        ),
    )


def system_card() -> rx.Component:
    """Archive health as three ratios that were measured. Public."""
    return panel_card(
        mn.stack(
            card_heading("activity", "System statistics"),
            _panel(
                DashboardState.loading_archive,
                DashboardState.archive_error,
                _meters(DashboardState.health, "warm"),
            ),
            gap="md",
            w="100%",
        )
    )


def disk_card() -> rx.Component:
    """What the archive occupies, path by path.

    The label, the size and the percentage are public; the absolute path is
    not, and only reaches this component when the state decided the caller may
    see it. There is no condition here — the row simply has no path in it.
    """
    return panel_card(
        mn.stack(
            card_heading("hard-drive", "Disk statistics"),
            _panel(
                DashboardState.loading_storage,
                DashboardState.storage_error,
                _meters(DashboardState.storage, "cool"),
            ),
            gap="md",
            w="100%",
        )
    )


def notifications_card() -> rx.Component:
    """What needs somebody's attention — or that nothing does.

    Administrators only, in full: every entry carries a mailbox address or the
    text of an error out of somebody's mail. Everybody else gets the same empty
    state a healthy archive gets, which is the point — a healthy archive must
    read as healthy, and a refusal must not read as a fault being hidden.
    """
    return panel_card(
        mn.stack(
            card_heading("bell", "Notifications"),
            _panel(
                DashboardState.loading_notifications,
                DashboardState.notifications_error,
                rx.cond(
                    DashboardState.has_notifications,
                    mn.stack(
                        rx.foreach(DashboardState.notifications, _notification),
                        gap=10,
                        w="100%",
                    ),
                    mn.box(
                        mn.empty_state(
                            icon=rx.icon("check", size=28),
                            title="Nothing needs attention",
                            description="No failed imports, no stalled jobs, no "
                            "account waiting to be reconnected.",
                            align="center",
                        ),
                        class_name="ma-panel",
                        w="100%",
                    ),
                ),
            ),
            gap="md",
            w="100%",
        )
    )


def services_card() -> rx.Component:
    """Whether the machinery is up — as five names and five ticks.

    No endpoint, no host, no port, no version. "The graph server is reachable"
    tells a visitor the archive works; "FalkorDB 4.0.9 at localhost:6379" tells
    them what to attack. The facts live on ``/admin/status``.
    """
    return panel_card(
        mn.stack(
            card_heading("server", "Services"),
            _while_reading(
                DashboardState.loading_services,
                # No panel. The checklist sits straight on the card, unlike
                # every other card body — which is what the reference draws, and
                # it is right: a tick is already its own mark and does not need
                # a second surface underneath to be legible.
                mn.stack(
                    rx.foreach(DashboardState.services, _service_entry),
                    gap=12,
                    px=8,
                    pb=8,
                    w="100%",
                ),
            ),
            gap="md",
            w="100%",
        )
    )


def messages_card() -> rx.Component:
    """How many messages the archive took in, per day."""
    return _chart_card(
        "chart-line",
        "Archived mails per day",
        mn.area_chart(
            data=DashboardState.messages_series,
            series=[{"name": "messages", "color": "var(--ma-chart-line)"}],
            **_CHART,
        ),
    )


def storage_card() -> rx.Component:
    """And how much room that took, over the same days and the same read."""
    return _chart_card(
        "database",
        "Storage used per day",
        mn.area_chart(
            data=DashboardState.storage_series,
            series=[{"name": "storage", "color": "var(--ma-chart-line)"}],
            unit=" GB",
            **_CHART,
        ),
    )


def _cards() -> rx.Component:
    """Three across, then a chart beside the checklist, then a chart.

    ``mn.grid`` rather than ``mn.simple_grid``: two of the six cards span two
    columns of three, and a simple grid has one width for every cell.
    """
    return mn.grid(
        mn.grid_col(system_card(), span={"base": 12, "md": 6, "lg": 4}),
        mn.grid_col(disk_card(), span={"base": 12, "md": 6, "lg": 4}),
        mn.grid_col(notifications_card(), span={"base": 12, "md": 12, "lg": 4}),
        mn.grid_col(messages_card(), span={"base": 12, "lg": 8}),
        mn.grid_col(services_card(), span={"base": 12, "lg": 4}),
        mn.grid_col(storage_card(), span={"base": 12, "lg": 8}),
        gutter="lg",
        w="100%",
    )


def _chart_card(icon: str, title: str, chart: rx.Component) -> rx.Component:
    """A chart under its heading, with the range switch on the same line."""
    return panel_card(
        mn.stack(
            mn.group(
                card_heading(icon, title),
                _range_switch(),
                justify="space-between",
                align="center",
                wrap="nowrap",
                w="100%",
            ),
            _panel(
                DashboardState.loading_series,
                DashboardState.series_error,
                mn.box(chart, class_name="ma-panel ma-panel-chart", w="100%"),
            ),
            gap="md",
            w="100%",
        )
    )


def _range_switch() -> rx.Component:
    """The same switch above both charts, bound to the one var they share."""
    return mn.segmented_control(
        data=RANGE_CHOICES,
        value=DashboardState.range,
        on_change=DashboardState.choose_range,
        size="xs",
        radius="md",
        class_name="ma-range",
    )


def _panel(loading: Any, error: Any, body: rx.Component) -> rx.Component:
    """One card's body: a placeholder, a reason, or the thing itself.

    Three states and not two. A card that showed nothing while its read was out
    would be making the same claim as a card whose read came back empty, and
    the whole point of a per-panel error string is that a reader can tell a
    quiet archive from a broken one.
    """
    return _while_reading(
        loading,
        rx.cond(
            error != "",
            mn.alert(
                error,
                color="red",
                variant="light",
                icon=rx.icon("triangle-alert", size=16),
                py="xs",
            ),
            body,
        ),
    )


def _while_reading(loading: Any, body: rx.Component) -> rx.Component:
    """A placeholder until the read comes back, then the body.

    The services card uses this on its own, with no error branch above it: a
    checklist row already says "could not ask" by going grey, so a failed read
    is a rendering that card has rather than a state it has to be replaced by.
    """
    return rx.cond(loading, mn.skeleton(h=96, radius="md", w="100%"), body)


def _meters(rows: Any, tone: CardTone) -> rx.Component:
    """Every row of one statistics card, inside one raised panel.

    One panel around all the rows rather than one per row: the design lifts a
    single white area out of the card's tint, and a panel per row would draw
    four borders where the reference draws one. Notifications are the deliberate
    exception — there the reference *does* give each entry its own panel, which
    is what separates a list of faults from a list of measurements.
    """
    return mn.stack(
        rx.foreach(rows, lambda row: _meter_row(row, tone)),
        gap=18,
        class_name="ma-panel",
        w="100%",
    )


def _meter_row(row: MeterView, tone: CardTone) -> rx.Component:
    """A tinted chip, a label, a striped bar and the percentage."""
    return mn.group(
        mn.center(rx.icon(row.icon, size=16), class_name=f"ma-chip ma-chip--{tone}"),
        mn.stack(
            mn.group(
                mn.text(row.label, class_name="ma-row-label"),
                mn.text(row.caption, class_name="ma-row-caption ma-tabular"),
                gap=6,
                align="baseline",
                wrap="nowrap",
            ),
            rx.cond(
                row.detail != "",
                mn.text(row.detail, class_name="ma-row-detail"),
                rx.fragment(),
            ),
            gap=2,
            class_name="ma-row-text",
        ),
        mn.progress(
            value=row.percent,
            color=_METER_COLORS[tone],
            size=8,
            radius="xl",
            striped=True,
            animated=False,
            class_name="ma-meter",
        ),
        mn.text(row.value, class_name="ma-row-value ma-tabular"),
        justify="space-between",
        align="center",
        # One line, always. The reference row is a chip, a label, a bar and a
        # figure across a single line, and that shape is what makes it read as a
        # meter; wrapping it turns one row into two and the card into a list of
        # stacked fragments. Three cards across a 1440px laptop leave this row
        # about 250px against the 450px the design was drawn at, so something
        # has to give — `.ma-meter` shrinks and the label ellipses, which is the
        # same truncation the notification text already uses.
        wrap="nowrap",
        gap="sm",
        w="100%",
    )


def _notification(row: Any) -> rx.Component:
    """One pending fault: a glyph, two clamped lines, and when it happened."""
    return mn.group(
        mn.center(
            rx.icon("info", size=14),
            class_name="ma-notice-dot",
        ),
        mn.stack(
            mn.text(row.message, class_name="ma-notice-text"),
            mn.text(row.when, class_name="ma-notice-when ma-tabular"),
            gap=2,
            style={"minWidth": 0, "flex": 1},
        ),
        align="flex-start",
        wrap="nowrap",
        gap="sm",
        class_name="ma-notice",
        w="100%",
    )


def _service_entry(row: Any, index: Any) -> rx.Component:
    """One checklist row, with the divider that may belong above it.

    §5c asks for a single dotted divider before the final group, and the state
    is what decides where that falls — ``DashboardState.services_split``. Doing
    the arithmetic here would mean subtracting inside an ``rx.foreach``, over a
    ``Var`` whose length is not a Python number.
    """
    return rx.fragment(
        rx.cond(
            index == DashboardState.services_split,
            mn.divider(variant="dotted", w="100%"),
            rx.fragment(),
        ),
        _service_row(row),
    )


def _service_row(row: Any) -> rx.Component:
    """A tick or a cross, and the name of the thing it is about."""
    return mn.group(
        rx.cond(
            row.up,
            mn.center(rx.icon("check", size=12), class_name="ma-service-dot"),
            mn.center(
                rx.icon("x", size=12), class_name="ma-service-dot ma-service-dot--off"
            ),
        ),
        mn.text(row.name, class_name="ma-row-label"),
        align="center",
        wrap="nowrap",
        gap="xs",
        w="100%",
    )


def _kpi_tile(icon: str, label: str, value: Any) -> rx.Component:
    """One figure on the band, with its name over it."""
    return mn.group(
        mn.center(rx.icon(icon, size=18), class_name="ma-kpi-icon"),
        mn.stack(
            mn.text(label, class_name="ma-kpi-label"),
            mn.text(value, class_name="ma-kpi-value"),
            gap=2,
            style={"minWidth": 0},
        ),
        align="center",
        wrap="nowrap",
        gap="sm",
        class_name="ma-kpi-tile",
    )


_METER_COLORS: dict[str, str] = {
    "warm": "var(--ma-meter-warm)",
    "cool": "var(--ma-meter-cool)",
    "neutral": "var(--ma-chart-line)",
}
"""The one place a colour is named in a component, and it names a variable.

Mantine's ``color`` prop takes a CSS colour rather than a class, so the bar's
fill cannot come off the stylesheet the way the track does. What goes across is
still the token's name and never its value.
"""
