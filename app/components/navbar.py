import appkit_mantine as mn
import reflex as rx


def app_navbar() -> rx.Component:
    return mn.box(
        mn.stack(
            mn.text("Project Kit Navbar", fw=700, size="md"),
            rx.link("Home", href="/"),
            rx.link("Users", href="/admin/users"),
        ),
        padding="2rem",
        border_right="1px solid",
        border_color="gray",
        height="100vh",
    )
