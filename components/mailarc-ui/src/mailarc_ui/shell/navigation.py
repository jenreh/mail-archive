"""The icon rail: the navigation table, and the one component that renders it.

Seventy-six pixels wide, no text in the column at all — a logo tile, two mono
section headings and a run of 44px icon buttons, each naming itself in a
tooltip to its right. Four decisions worth stating, because each has an
obvious alternative that does not work here.

**The active item comes from the router, not from a state.** A state that
recorded which entry was clicked would be a second copy of a fact the browser
already holds, and the two disagree the moment somebody uses the back button
or opens a link in a new tab. ``rx.State.router.page.path`` is the address bar.

**There is no gate.** The archive ships as a desktop application with no
sign-in, so every entry renders for whoever opened the window. What used to be
``admin_only`` data on the item is gone rather than defaulted to ``False``: a
flag nothing reads is a permission somebody will believe is enforced.

**The administration is a popover, not three more icons.** Three maintenance
pages in the same column as the three a person actually works in would make
the rail read as six equals. One ``mn.menu`` to the right holds them,
labelled, and the trigger lights up while any of the three is the current page.

**The look comes from the stylesheet, keyed on ``data-active``.** The
alternative — a per-item ``style`` dict assembled with ``rx.cond`` — puts one
colour at a time across the Var boundary, and the active item is not one
colour: it is a background, a border, a radius, a shadow and a text colour
together. One class in ``mail-archive.css`` says all of that once.

**Nothing inside an ``rx.link`` renders a second anchor.** React refuses
nested interactive elements out loud on every page load — "<a> cannot contain
a nested <a>. This will cause a hydration error" — which is what the previous
sidebar hit with Mantine's ``NavLink``. Every rail item is an ``mn.box``, a
plain ``div``, and the router link around it is the only thing that navigates.
The administration's entries navigate with ``rx.redirect`` from a menu item
instead, for the same reason: a link inside a menu item is that bug again.
"""

from typing import Any

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.shell import routes
from mailarc_ui.shell.model import NavItem, NavSection

NAVBAR_WIDTH = 76
"""What the shell reserves for the rail, in pixels."""

INDEX_ALIAS = "/index"
"""The second spelling Reflex serves the index page under.

Which of the two the router reports depends on how the page was reached, so
the item answering at ``/`` has to accept both or it goes dark on a reload.
"""

RAIL_SECTIONS: tuple[NavSection, ...] = (
    NavSection(
        label="Menu",
        items=(
            NavItem(label="Search", href=routes.SEARCH, icon="mail-search"),
            NavItem(label="Dashboard", href=routes.DASHBOARD, icon="layout-dashboard"),
            NavItem(label="Insights", href=routes.INSIGHTS, icon="chart-line"),
        ),
    ),
    NavSection(
        label="Admin",
        items=(
            NavItem(label="Mail accounts", href=routes.ACCOUNTS, icon="at-sign"),
            NavItem(label="Embedder", href=routes.EMBEDDER, icon="brain"),
            NavItem(label="Graph status", href=routes.GRAPH_STATUS, icon="database"),
        ),
    ),
)


def _is_active(href: str) -> Any:
    """Whether the browser is on this entry's page right now."""
    current = rx.State.router.page.path
    if href == routes.SEARCH:
        return (current == routes.SEARCH) | (current == INDEX_ALIAS)
    return current == href


def _rail_icon(item: NavItem, active: Any) -> rx.Component:

    return mn.box(
        rx.icon(item.icon, size=20),
        class_name="ma-rail-item",
        custom_attrs={"data-active": active},
    )


def _rail_link(item: NavItem) -> rx.Component:

    return mn.tooltip(
        rx.link(
            _rail_icon(item, _is_active(item.href)),
            href=item.href,
            underline="none",
            style={"display": "flex"},
        ),
        label=item.label,
        position="right",
        offset=10,
    )


def _section_label(section: NavSection) -> rx.Component:
    """``MENU`` / ``ADMIN`` — mono, 10px, uppercase from the stylesheet."""
    return mn.text(section.label, class_name="ma-rail-label")


def _menu_section(section: NavSection) -> rx.Component:
    return mn.app_shell.section(
        mn.stack(
            _section_label(section),
            *[_rail_link(item) for item in section.items],
            gap=8,
            align="center",
        ),
    )


def _logo() -> rx.Component:

    return mn.app_shell.section(
        mn.box(rx.icon("mails", size=22), class_name="ma-rail-logo"),
        mb=18,
    )


def _bottom_slot() -> rx.Component:

    return mn.app_shell.section(
        mn.tooltip(
            mn.unstyled_button(
                rx.color_mode_cond(
                    light=rx.icon("moon", size=18),
                    dark=rx.icon("sun", size=18),
                ),
                class_name="ma-rail-item",
                on_click=rx.toggle_color_mode,
            ),
            label="Light or dark",
            position="right",
            offset=10,
        ),
    )


def app_sidebar() -> rx.Component:
    """The whole rail, ready to hand to ``mn.app_shell``.

    The ``grow=True`` section in the middle is what pins the bottom slot to the
    foot of the rail however many entries sit above it.
    """
    menu, administration = RAIL_SECTIONS
    return mn.app_shell.navbar(
        _logo(),
        _menu_section(menu),
        _menu_section(administration),
        mn.app_shell.section(grow=True),
        _bottom_slot(),
        class_name="ma-rail",
        with_border=False,
    )
