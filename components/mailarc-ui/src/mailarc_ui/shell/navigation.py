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

**The administration is a popover, not four more icons.** Four maintenance
pages in the same column as the three a person actually works in would make
the rail read as seven equals. One ``mn.menu`` to the right holds them,
labelled, and the trigger lights up while any of the four is the current page.

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
            NavItem(label="Review", href=routes.REVIEW, icon="mail"),
            NavItem(label="Mail accounts", href=routes.ACCOUNTS, icon="at-sign"),
            NavItem(label="Embedder", href=routes.EMBEDDER, icon="brain"),
            NavItem(label="Graph status", href=routes.GRAPH_STATUS, icon="database"),
        ),
    ),
)
"""What the rail offers, top to bottom.

Two sections and they are two different kinds of thing. ``MENU`` is where a
person works — the search they arrive at, what the archive holds, what was
derived from it — and each entry is an icon of its own. ``ADMIN`` is what an
operator maintains, and it is one icon with a popover behind it.
"""

ADMIN_ICON = "settings-2"
"""The trigger the administration's popover hangs from."""


def _is_active(href: str) -> Any:
    """Whether the browser is on this entry's page right now."""
    current = rx.State.router.page.path
    if href == routes.SEARCH:
        return (current == routes.SEARCH) | (current == INDEX_ALIAS)
    return current == href


def _administration_is_active() -> Any:
    """Whether any page the popover holds is the current one.

    Written out as four equalities rather than as a ``startswith("/admin/")``
    on the router path: the entries are data, the prefix is a coincidence of
    how they are spelled today, and a rail that lit up for any path beginning
    ``/admin/`` would light up for one this menu does not offer.
    """
    _, administration = RAIL_SECTIONS
    matches = [_is_active(item.href) for item in administration.items]
    active = matches[0]
    for match in matches[1:]:
        active = active | match
    return active


def _rail_icon(item: NavItem, active: Any) -> rx.Component:
    """The 44px square itself: an icon in a box the stylesheet dresses.

    A ``div`` and not a button, because this sits inside the router link that
    makes it navigate. ``data-active`` reaches the DOM through ``custom_attrs``
    — Reflex folds an undeclared keyword into the ``css`` prop, where it would
    become a bogus CSS key rather than an attribute the stylesheet can match.
    """
    return mn.box(
        rx.icon(item.icon, size=20),
        class_name="ma-rail-item",
        custom_attrs={"data-active": active},
    )


def _rail_link(item: NavItem) -> rx.Component:
    """One menu entry: the square, the route it goes to, the name it answers to.

    The tooltip is the only place this entry's label is ever shown — the rail
    is too narrow for text — so it is not decoration, it is the label.
    """
    return mn.tooltip(
        rx.link(
            _rail_icon(item, _is_active(item.href)),
            href=item.href,
            underline="none",
            # An anchor is inline, and an inline box around a 44px square
            # inherits a line box that leaves a stripe of dead space under
            # every item. `flex` is what makes the anchor exactly its square.
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


def _administration_item(item: NavItem) -> rx.Component:
    """One row of the popover: an icon, a name, and the page it opens.

    ``rx.redirect`` rather than an ``rx.link`` around the row: a menu item is
    already a button, and an anchor inside one is the nested-interactive
    element React complains about on every render.
    """
    return mn.menu.item(
        item.label,
        left_section=rx.icon(item.icon, size=16),
        on_click=rx.redirect(item.href),
    )


def _administration_section(section: NavSection) -> rx.Component:
    """The four maintenance pages, behind one icon and a popover to its right."""
    return mn.app_shell.section(
        mn.stack(
            _section_label(section),
            # The active state rides on a hidden marker rather than on the
            # trigger, and the trigger is a button rather than a box. Both are
            # forced by ``Menu.Target``, which opens the dropdown by cloning
            # its child with an ``onClick`` and a ``ref``: a box forwards
            # neither, and *any* element carrying a state-dependent attribute
            # is compiled by Reflex into a ``memo(({children}) => …)`` wrapper
            # that takes children and discards every other prop. Either one
            # leaves the menu unopenable — measured in the browser, where the
            # trigger rendered without so much as an ``aria-haspopup``. The
            # marker is a sibling the CSS reaches with ``~``; see
            # ``.ma-rail-flag`` in ``mail-archive.css``.
            mn.box(
                class_name="ma-rail-flag",
                custom_attrs={"data-active": _administration_is_active()},
            ),
            mn.menu(
                mn.menu.target(
                    mn.unstyled_button(
                        rx.icon(ADMIN_ICON, size=20),
                        class_name="ma-rail-item",
                        aria_label="Administration",
                    ),
                ),
                mn.menu.dropdown(
                    mn.menu.label(section.label),
                    *[_administration_item(item) for item in section.items],
                ),
                position="right-start",
                offset=10,
                width=200,
                shadow="md",
            ),
            gap=8,
            align="center",
        ),
    )


def _logo() -> rx.Component:
    """The orange rounded square at the top of the rail.

    The application's mark and not its name: this ships as a desktop
    application, where the window already carries the name and the icon sits
    in the dock, so a wordmark drawn inside the rail is the title bar said
    twice — and there is no room for one at 76px anyway.
    """
    return mn.app_shell.section(
        mn.box(rx.icon("mails", size=22), class_name="ma-rail-logo"),
        mb=18,
    )


def _bottom_slot() -> rx.Component:
    """The foot of the rail: light or dark, and nothing else.

    Mantine takes its colour scheme from Reflex's — ``appkit_mantine`` forces
    ``force_color_scheme`` off ``rx.color_mode`` — so one toggle moves both
    halves of the design, the ``--ma-*`` tokens' dark block included.
    """
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
        _administration_section(administration),
        mn.app_shell.section(grow=True),
        _bottom_slot(),
        class_name="ma-rail",
    )
