"""The card that offers the conversation repair, and reports what it did.

Layout and nothing else. Every value comes from
:class:`~mailarc_ui.repair.state.ConversationRepairState`, and every piece is a
``kit`` component, so this card is drawn from the same vocabulary as the three
it sits under on the graph status page.

One card and not a page: the repair is one button over one number, and giving
it an address of its own would put a fourth icon in the rail for something a
person presses once.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import (
    card_heading,
    message,
    panel_card,
    primary_button,
    soft_button,
    status_badge,
)
from mailarc_ui.repair.model import EXPLANATION, REPAIR_ICON, TITLE
from mailarc_ui.repair.state import ConversationRepairState


def _header() -> rx.Component:
    """The title, and the state of the job when there is one."""
    return mn.group(
        card_heading(REPAIR_ICON, TITLE),
        rx.cond(
            ConversationRepairState.job.job_id > 0,
            status_badge(
                ConversationRepairState.job.status,
                ConversationRepairState.job.status_color,
            ),
            rx.fragment(),
        ),
        justify="space-between",
        align="center",
        w="100%",
    )


def _controls() -> rx.Component:
    """Repair, and read the count again — everything that starts work.

    No cancel. A repair is idempotent and over in seconds, so a stop button
    would be a control for work that has already finished; the worker's handler
    says the same thing from the other side.
    """
    return mn.group(
        primary_button(
            "Repair conversations",
            on_click=ConversationRepairState.start_repair,
            loading=ConversationRepairState.starting
            | ConversationRepairState.job.active,
            disabled=~ConversationRepairState.can_repair,
            left_section=rx.icon("wand-sparkles", size=14),
            size="xs",
        ),
        soft_button(
            "Refresh",
            on_click=ConversationRepairState.load,
            loading=ConversationRepairState.loading,
            left_section=rx.icon("refresh-cw", size=14),
            size="xs",
        ),
        gap="sm",
    )


def repair_card() -> rx.Component:
    """How much of the archive is outside a conversation, and the fix.

    The count leads, because it is the fact; the explanation follows it,
    because "735 messages sit in no conversation" without a reason reads as
    data loss rather than as a missing edge. What a finished run did is shown
    as a sentence and not as a bar — the row carries a result, not a position.
    """
    return panel_card(
        mn.stack(
            _header(),
            mn.text(
                ConversationRepairState.outside_message,
                size="sm",
                fw=600,
            ),
            mn.text(EXPLANATION, size="xs", c="dimmed"),
            rx.cond(
                ConversationRepairState.error != "",
                message(ConversationRepairState.error, "warning"),
                rx.fragment(),
            ),
            rx.cond(
                ConversationRepairState.notice != "",
                message(ConversationRepairState.notice, "note"),
                rx.fragment(),
            ),
            rx.cond(
                ConversationRepairState.result_message != "",
                message(ConversationRepairState.result_message, "success"),
                rx.fragment(),
            ),
            rx.cond(
                ConversationRepairState.job.error != "",
                message(ConversationRepairState.job.error, "failure"),
                rx.fragment(),
            ),
            _controls(),
            gap="sm",
            w="100%",
        ),
        on_unmount=ConversationRepairState.stop_polling,
    )
