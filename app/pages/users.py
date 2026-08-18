import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.components.components import requires_admin
from appkit_user.authentication.templates import authenticated
from appkit_user.user_management.components.user import (
    add_user_button,
    add_user_modal,
    edit_user_modal,
    search_user_input,
    user_table_view,
)
from appkit_user.user_management.states.user_states import UserState

from app.components.navbar import app_navbar
from app.roles import ALL_ROLES


@authenticated(
    route="/admin/users",
    title="Benutzerverwaltung",
    navbar=app_navbar(),
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positives.
    on_load=[UserState.set_available_roles(ALL_ROLES)],  # ty: ignore[invalid-argument-type, missing-argument]
)
def users_page() -> rx.Component:
    additional_components = []

    return requires_admin(
        add_user_modal(),
        edit_user_modal(),
        mn.stack(
            mn.title("Benutzerverwaltung"),
            mn.group(
                add_user_button(),
                search_user_input(),
            ),
            user_table_view(additional_components=additional_components),
            width="100%",
            max_width="1200px",
            spacing="6",
            pr="2rem",
            pl="2rem",
        ),
    )
