"""The import panel: start, a bar that climbs, cancel, and what ran before.

Deliberately plain — phase 4 exists so an import can be triggered and watched,
and it will be replaced (§10). Nothing here builds a queue or reads
configuration; every value comes from :class:`ImportJobState`.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.imports.state import ImportJobRow, ImportJobState
from mailarc_ui.kit import panel_card


def import_controls() -> rx.Component:
    """The two buttons. Cancel is live only while the job can still hear it."""
    return mn.group(
        mn.button(
            "Start import",
            on_click=ImportJobState.start_import,
            loading=ImportJobState.starting,
            disabled=~ImportJobState.can_start,
            left_section=rx.icon("download", size=14),
            variant="filled",
            size="xs",
        ),
        mn.button(
            "Cancel",
            on_click=ImportJobState.cancel_import,
            loading=ImportJobState.cancelling,
            disabled=~ImportJobState.can_cancel,
            left_section=rx.icon("square", size=14),
            variant="light",
            color="red",
            size="xs",
        ),
        gap="sm",
    )


def import_progress() -> rx.Component:
    """The bar, with the counts next to it so a percentage never stands alone."""
    return mn.group(
        mn.progress(
            value=ImportJobState.job.percent,
            color="blue",
            size="lg",
            striped=ImportJobState.job.active,
            animated=ImportJobState.job.active,
            flex="1",
        ),
        mn.text(
            ImportJobState.job.percent_label,
            size="sm",
            fw=600,
            w=52,
            ta="right",
            class_name="ma-tabular",
        ),
        mn.text(
            ImportJobState.job.counts_label,
            size="sm",
            c="dimmed",
            class_name="ma-tabular",
        ),
        gap="sm",
        align="center",
        w="100%",
    )


def recent_jobs() -> rx.Component:
    """The short history: what this panel started and how it ended."""
    return rx.cond(
        ImportJobState.has_recent,
        mn.table(
            mn.table.thead(
                mn.table.tr(
                    mn.table.th("Job"),
                    mn.table.th("State"),
                    mn.table.th("Progress"),
                    mn.table.th("Messages"),
                    mn.table.th("Error"),
                ),
            ),
            mn.table.tbody(rx.foreach(ImportJobState.recent, _job_row)),
            striped=True,
            highlight_on_hover=True,
            tabular_nums=True,
        ),
        mn.empty_state(
            icon=rx.icon("inbox", size=28),
            title="No imports yet",
            description="Start one and it shows up here.",
            align="center",
        ),
    )


def import_panel() -> rx.Component:
    """The whole thing in one card, for a page to drop in."""
    return panel_card(
        mn.stack(
            mn.group(
                mn.text("Import", fw=600, size="sm"),
                import_controls(),
                justify="space-between",
                align="center",
                w="100%",
            ),
            import_progress(),
            rx.cond(
                ImportJobState.message != "",
                mn.alert(ImportJobState.message, color="yellow", variant="light"),
                mn.text(""),
            ),
            rx.cond(
                ImportJobState.job.error != "",
                mn.alert(
                    ImportJobState.job.error,
                    title="The import stopped with an error",
                    color="red",
                    variant="light",
                    icon=rx.icon("triangle-alert", size=16),
                ),
                mn.text(""),
            ),
            recent_jobs(),
            gap="md",
        ),
    )


def _job_row(row: ImportJobRow) -> rx.Component:
    return mn.table.tr(
        mn.table.td(row.job_id),
        mn.table.td(
            mn.badge(row.status, color=row.status_color, variant="light", size="sm")
        ),
        mn.table.td(row.percent_label),
        mn.table.td(row.counts_label),
        mn.table.td(row.error),
    )
