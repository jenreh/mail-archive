"""The import panel: start, a bar that climbs, cancel, and what ran before.

Deliberately plain — phase 4 exists so an import can be triggered and watched,
and it will be replaced (§10). Nothing here builds a queue or reads
configuration; every value comes from :class:`ImportJobState`.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.imports.state import ImportJobRow, ImportJobState
from mailarc_ui.kit import (
    empty_panel,
    job_progress,
    message,
    primary_button,
    scroll_table,
    soft_button,
    status_badge,
)

RECENT_ROWS = 5
"""How much of the history this panel shows before it scrolls.

Fewer than the twelve a listing gets: this sits under the mailbox it belongs
to, and a history taller than the mailbox above it would be the page's subject.
"""


def import_controls() -> rx.Component:
    """The two buttons. Cancel is live only while the job can still hear it."""
    return mn.group(
        primary_button(
            "Start import",
            on_click=ImportJobState.start_import,
            loading=ImportJobState.starting,
            disabled=~ImportJobState.can_start,
            left_section=rx.icon("download", size=14),
            size="xs",
        ),
        soft_button(
            "Cancel",
            on_click=ImportJobState.cancel_import,
            loading=ImportJobState.cancelling,
            disabled=~ImportJobState.can_cancel,
            left_section=rx.icon("square", size=14),
            color="red",
            size="xs",
        ),
        gap="sm",
    )


def import_progress() -> rx.Component:
    """The bar, with the counts next to it so a percentage never stands alone.

    The status badge arrived with the kit row. This panel is the one place a
    job is *started*, and it was the one place that did not say when the job
    had failed — the badge was written into the other two copies of this row
    and never back into this one.
    """
    return job_progress(
        ImportJobState.job.percent,
        ImportJobState.job.percent_label,
        ImportJobState.job.counts_label,
        ImportJobState.job.active,
        status=ImportJobState.job.status,
        status_color=ImportJobState.job.status_color,
    )


def recent_jobs() -> rx.Component:
    """The short history: what this panel started and how it ended."""
    return rx.cond(
        ImportJobState.has_recent,
        scroll_table(
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
            rows=RECENT_ROWS,
        ),
        empty_panel("inbox", "No imports yet", "Start one and it shows up here."),
    )


def import_panel() -> rx.Component:
    """The whole thing as one section, for a page to drop into a column.

    No card of its own. It used to have one, back when the accounts page was a
    stack of full-width cards; it now sits under the mailbox it imports, in
    that mailbox's column, and a card inside a column would draw a second
    surface on a surface.
    """
    return mn.stack(
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
            message(ImportJobState.message, "warning"),
            rx.fragment(),
        ),
        rx.cond(
            ImportJobState.job.error != "",
            message(
                ImportJobState.job.error,
                "failure",
                title="The import stopped with an error",
            ),
            rx.fragment(),
        ),
        recent_jobs(),
        gap="md",
        w="100%",
    )


def _job_row(row: ImportJobRow) -> rx.Component:
    return mn.table.tr(
        mn.table.td(row.job_id),
        mn.table.td(status_badge(row.status, row.status_color)),
        mn.table.td(row.percent_label),
        mn.table.td(row.counts_label),
        mn.table.td(row.error),
    )
