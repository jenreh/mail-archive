"""A signed-in person's own account: their details and their password.

appkit ships this page as a factory — ``create_profile_page(navbar, …)`` — and
the navbar it takes is the reason the page had to be rebuilt rather than
called. That argument goes into ``@authenticated``'s hard-wired ``mn.flex``
layout, so the profile page was the one page of this application that could not
be given the archive's shell: it would have kept the generated scaffolding's
navbar after everything else stopped using it.

So the factory's body is used and its layout is not, which is what voyager does
with the same page for the same reason. ``user_profile_view`` is appkit's own
component and does the work; this module gives it a route, a heading and the
shell.

Not admin-gated on purpose. It is every signed-in person's own account, and
``admin_only`` here would take a regular user's password form away from them.
"""

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import authenticated_page
from appkit_user.user_management.components.user_profile import user_profile_view

from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_app

ROUTE = routes.PROFILE
"""Where this page lives; the sidebar reads the same constant."""


@authenticated_page(
    route=ROUTE,
    title="Profil",
    description="Your account details and your password",
    template=mailarc_app,
)
def profile_page() -> rx.Component:
    return mn.stack(
        page_header("Profil", "Ihre Angaben und Ihr Passwort"),
        user_profile_view(class_name="w-full gap-6"),
        gap=PAGE_GAP,
        w="100%",
        maw=800,
        mx="auto",
        p=PAGE_PADDING,
    )
