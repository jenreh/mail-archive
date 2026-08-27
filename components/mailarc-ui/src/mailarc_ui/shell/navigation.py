"""The sidebar: the navigation table, and the one component that renders it.

Three decisions worth stating, because each has an obvious alternative that
does not work here.

**The active row comes from the router, not from a state.** A state that
recorded which entry was clicked would be a second copy of a fact the browser
already holds, and the two disagree the moment somebody uses the back button
or opens a link in a new tab. ``rx.State.router.page.path`` is the address bar.

**The gate is data on the item.** ``/`` is public, so the sidebar renders for
people who are not signed in at all, and every ``/admin/*`` entry has to be
absent from that render rather than merely refused on click. Declaring it on
the item means the question "who sees this?" is answered where the entry is
written, and :func:`_gated` is the only code that has to know how.

**The look comes from the stylesheet, keyed on Mantine's own ``data-active``.**
The alternative — a per-item ``style`` dict assembled with ``rx.cond`` — puts
one colour at a time across the Var boundary, and the active row is not one
colour: it is a background, a border, a radius, a shadow and a font weight
together. One class in ``mail-archive.css`` says all of that once.
"""

from typing import Any

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.components.components import (
    requires_admin,
    requires_role,
)
from appkit_user.authentication.states import LOGIN_ROUTE, LoginState, UserSession

from mailarc_ui.shell import routes
from mailarc_ui.shell.model import NavItem, NavSection

NAVBAR_WIDTH = 300
"""What the shell reserves for the sidebar, in pixels."""

NAV_SECTIONS: tuple[NavSection, ...] = (
    NavSection(
        items=(
            NavItem(
                label="Dashboard",
                href=routes.DASHBOARD,
                icon="layout-dashboard",
            ),
            NavItem(
                label="Review",
                href=routes.REVIEW,
                icon="mail",
                admin_only=True,
            ),
            NavItem(
                label="Insights",
                href=routes.INSIGHTS,
                icon="chart-line",
                admin_only=True,
            ),
        )
    ),
    NavSection(
        items=(
            NavItem(
                label="Mail accounts",
                href=routes.ACCOUNTS,
                icon="at-sign",
                admin_only=True,
            ),
            NavItem(
                label="Embedder",
                href=routes.EMBEDDER,
                icon="brain",
                admin_only=True,
            ),
            NavItem(
                label="Graph status",
                href=routes.GRAPH_STATUS,
                icon="database",
                admin_only=True,
            ),
            NavItem(
                label="Users",
                href=routes.USERS,
                icon="users",
                admin_only=True,
            ),
        )
    ),
    NavSection(
        items=(
            # Signed in, and *not* admin-gated, which is the one place this
            # table departs from "everything except `/` is administration".
            # `/profile` is where a person changes their own password, and
            # `pages/profile.py` is decorated to match: an `admin_only` link
            # here would hide a page a regular account holder is entitled to
            # and can still reach by typing the path.
            NavItem(
                label="Profile",
                href=routes.PROFILE,
                icon="user-round",
                requires_login=True,
            ),
        )
    ),
)
"""What the sidebar shows, top to bottom.

Three sections: what a person looks at, what an administrator maintains, and
the secondary group that sits at the bottom above the user footer. The order
inside a section is the order somebody works in — the archive first, then what
was derived from it, then the machinery underneath.
"""


def _is_active(href: str) -> Any:
    """Whether the browser is on this entry's page right now."""
    current = rx.State.router.page.path
    if href == routes.DASHBOARD:
        # Reflex serves the index under both spellings, and which one the
        # router reports depends on how the page was reached.
        return (current == routes.DASHBOARD) | (current == "/index")
    return current == href


def _gated(item: NavItem, component: rx.Component) -> rx.Component:
    """Apply whatever the item declared about who may see it.

    Nested rather than exclusive, so an entry that is both administrative and
    role-scoped gets both checks. No gate at all is the default, and it is
    what ``/`` relies on.
    """
    if item.requires_role is not None:
        component = requires_role(component, role=item.requires_role)
    if item.admin_only:
        component = requires_admin(component)
    if item.requires_login:
        component = rx.cond(LoginState.is_authenticated, component, rx.fragment())
    return component


def _nav_link(item: NavItem) -> rx.Component:
    """One row, wrapped in the router link that makes it navigate.

    ``mn.nav_link`` declares no ``href``: on its own it is a styled label with
    an ``on_click``, and a sidebar built out of them looks entirely correct and
    goes nowhere. The ``rx.link`` around it is what puts a real router target
    into the rendered tree.

    **``component="div"`` is what keeps that legal.** Mantine's ``NavLink`` is
    polymorphic over ``'a'``, so by default the row renders an anchor *inside*
    the router's anchor — React refuses nested interactive elements and says so
    on every page load ("<a> cannot contain a nested <a>. This will cause a
    hydration error"), which was measured in a browser rather than reasoned
    about. Rendered as a ``div`` the row is a plain box and the link around it
    is the only thing that navigates, which is what it was always meant to be.

    It goes through ``custom_attrs`` and not as a keyword, and that is not
    decoration: ``component`` is not a declared prop of this wrapper, and
    Reflex folds an undeclared keyword into the ``css`` prop — measured,
    ``mn.nav_link(component="div")`` renders ``css:({["component"]:"div"})``
    and a bogus CSS key. ``custom_attrs`` is the door that puts a real React
    prop on the component.
    """
    return _gated(
        item,
        rx.link(
            mn.nav_link(
                label=item.label,
                left_section=rx.icon(item.icon, size=18),
                active=_is_active(item.href),
                variant="subtle",
                class_name="ma-nav-link",
                custom_attrs={"component": "div"},
            ),
            href=item.href,
            underline="none",
            class_name="ma-nav-anchor",
        ),
    )


def _section(section: NavSection) -> rx.Component:
    return mn.app_shell.section(
        mn.stack(*[_nav_link(item) for item in section.items], gap=8),
    )


def _divider() -> rx.Component:
    return mn.app_shell.section(
        mn.divider(variant="dotted", class_name="ma-nav-divider")
    )


def _signed_in() -> rx.Component:
    """Who is signed in, and the way out.

    Both lines are single-line with an ellipsis, from the stylesheet: a display
    name and a mail address are both arbitrarily long, and a sidebar that grows
    a third line for one account and not another is a layout that depends on
    who is looking at it.

    ``UserSession.user`` is typed ``User | None`` and is read here as a Var, so
    the attribute accesses are suppressed rather than guarded: Reflex compiles
    them to optional chaining (``user?.["name"]``) and renders an empty string
    when there is no user. There is no user to read only while signed out, and
    ``rx.cond`` has already chosen the other branch by then.
    """
    return mn.group(
        mn.avatar(
            name=UserSession.user.name,  # ty: ignore[unresolved-attribute]
            size=36,
            radius="xl",
            color="blue",
            variant="filled",
        ),
        mn.stack(
            mn.text(
                UserSession.user.name,  # ty: ignore[unresolved-attribute]
                class_name="ma-user-name",
            ),
            mn.text(
                UserSession.user.email,  # ty: ignore[unresolved-attribute]
                class_name="ma-user-mail",
            ),
            gap=0,
            style={"minWidth": 0, "flex": 1},
        ),
        mn.action_icon(
            rx.icon("log-out", size=16),
            variant="subtle",
            color="gray",
            on_click=LoginState.logout,
        ),
        gap="sm",
        wrap="nowrap",
        w="100%",
    )


def _signed_out() -> rx.Component:
    return mn.button(
        "Sign in",
        left_section=rx.icon("log-in", size=16),
        variant="light",
        w="100%",
        on_click=rx.redirect(LOGIN_ROUTE),
    )


def _user_footer() -> rx.Component:
    return mn.app_shell.section(
        rx.cond(LoginState.is_authenticated, _signed_in(), _signed_out()),
        p="12px",
    )


def app_sidebar() -> rx.Component:
    """The whole sidebar, ready to hand to ``mn.app_shell``.

    The ``grow=True`` section in the middle is what pins the secondary group
    and the user footer to the bottom of the navbar however many entries the
    person looking is allowed to see — which is the point, because that number
    changes with who is signed in.
    """
    primary, administrative, secondary = NAV_SECTIONS
    return mn.app_shell.navbar(
        # No wordmark. This ships as a desktop application, where the window
        # already carries the application's name and its icon sits in the dock —
        # a second one drawn inside the sidebar is the title bar said twice.
        _section(primary),
        _divider(),
        _section(administrative),
        mn.app_shell.section(grow=True),
        _section(secondary),
        _divider(),
        _user_footer(),
        pt="16px",
        pb="8px",
        class_name="ma-navbar",
    )
