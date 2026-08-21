"""Look into the synced mail: the list on the left, the original on the right.

Layout and nothing else. The whole panel is one component ``mailarc-ui``
exports — :func:`review_panel` — and this module only gives it a route, a
title and the height it needs to scroll inside.
"""

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import authenticated

from app.components.navbar import app_navbar
from mailarc_ui.review import MessageReviewState, review_panel

ROUTE = "/mail/review"
"""Where this page lives; ``app/components/navbar.py`` links here."""


@authenticated(
    route=ROUTE,
    title="Review",
    description="Look into the synced messages and their raw source",
    navbar=app_navbar(),
    with_header=False,
    # Admin-only, for the reason `/mail/accounts` is: the archive is every
    # mailbox of the installation, which is everybody's private mail.
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positive.
    on_load=[MessageReviewState.load],  # ty: ignore[invalid-argument-type]
)
def mail_review_page() -> rx.Component:
    return mn.stack(
        mn.group(
            mn.stack(
                mn.title("Review", order=1),
                mn.text(
                    "What the import wrote, newest first. Pick a message to see "
                    "it the way it came off the wire.",
                    c="dimmed",
                    size="sm",
                ),
                gap="xs",
            ),
            mn.group(
                mn.text(MessageReviewState.count_label, size="sm", c="dimmed"),
                mn.button(
                    "Refresh",
                    on_click=MessageReviewState.load,
                    loading=MessageReviewState.loading,
                    variant="light",
                    size="xs",
                    left_section=rx.icon("refresh-cw", size=14),
                ),
                gap="sm",
                align="center",
            ),
            justify="space-between",
            align="flex-start",
            w="100%",
        ),
        rx.cond(
            MessageReviewState.error != "",
            mn.alert(
                MessageReviewState.error,
                title="The archive did not answer",
                color="red",
                variant="light",
                icon=rx.icon("triangle-alert", size=16),
            ),
            mn.text(""),
        ),
        mn.box(review_panel(), flex="1", w="100%", style={"minHeight": 0}),
        gap="md",
        w="100%",
        h="100%",
    )
