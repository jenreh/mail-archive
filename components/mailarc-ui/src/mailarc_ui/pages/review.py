"""Look into the synced mail: the list on the left, the original on the right.

Layout and nothing else. The whole panel is one component ``mailarc-ui``
exports — :func:`review_panel` — and this module only gives it a route, a
title and the height it needs to scroll inside.

The height is why this is the one page on ``mailarc_full_app``. Both columns
scroll on their own, so they need a parent with a definite height rather than
one that grows to fit them; it used to get that from ``navbar_layout``'s
hard-coded ``100vh`` column, and without the full-height template the reader
would silently turn into one very long page.
"""

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import authenticated_page

from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.review import MessageReviewState, review_panel
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_full_app

ROUTE = routes.REVIEW
"""Where this page lives; the sidebar reads the same constant."""


def _toolbar() -> rx.Component:
    return mn.group(
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
    )


@authenticated_page(
    route=ROUTE,
    title="Review",
    description="Look into the synced messages and their raw source",
    template=mailarc_full_app,
    # Admin-only, for the reason `/admin/accounts` is: the archive is every
    # mailbox of the installation, which is everybody's private mail.
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positive.
    on_load=[MessageReviewState.load],  # ty: ignore[invalid-argument-type]
)
def review_page() -> rx.Component:
    return mn.stack(
        page_header(
            "Review",
            "What the import wrote, newest first. Pick a message to see it "
            "the way it came off the wire.",
            actions=_toolbar(),
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
        gap=PAGE_GAP,
        w="100%",
        h="100%",
        p=PAGE_PADDING,
    )
