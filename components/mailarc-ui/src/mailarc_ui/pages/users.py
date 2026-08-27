"""Who may sign in to this installation, and with which roles.

appkit owns every piece of this page — the table, the two modals, the search
box and the button — so the module is a route, a gate and an order. The one
decision it makes of its own is the role catalogue it primes ``UserState``
with, and that catalogue now comes from ``mailarc_core``: roles are
archive-wide policy that a worker and a CLI obey as much as a browser does.
"""

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.components.components import requires_admin
from appkit_user.authentication.templates import authenticated_page
from appkit_user.user_management.components.user import (
    add_user_button,
    add_user_modal,
    edit_user_modal,
    search_user_input,
    user_table_view,
)
from appkit_user.user_management.states.user_states import UserState

from mailarc_core import ALL_ROLES
from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_app

ROUTE = routes.USERS
"""Where this page lives; the sidebar reads the same constant."""


@authenticated_page(
    route=ROUTE,
    title="Benutzerverwaltung",
    template=mailarc_app,
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positives.
    on_load=[UserState.set_available_roles(ALL_ROLES)],  # ty: ignore[invalid-argument-type]
)
def users_page() -> rx.Component:
    additional_components: list[rx.Component] = []

    return requires_admin(
        add_user_modal(),
        edit_user_modal(),
        mn.stack(
            page_header("Benutzerverwaltung"),
            mn.group(
                add_user_button(),
                search_user_input(),
            ),
            user_table_view(additional_components=additional_components),
            gap=PAGE_GAP,
            w="100%",
            maw=1200,
            mx="auto",
            p=PAGE_PADDING,
        ),
    )
