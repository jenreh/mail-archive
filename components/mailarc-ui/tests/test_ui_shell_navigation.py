"""The shell: what the sidebar links to, who may see it, and what `/` is not.

Three things are worth pinning here and they are all things a reading of the
source cannot settle.

The first is that a navigation item reaches the browser as a *router* link.
``mn.nav_link`` takes no ``href`` — verified by rendering one: its props are
``label``, ``active`` and ``variant``, and nothing else — so every entry is
wrapped in an ``rx.link``, and the proof is the ``to:"…"`` prop on the
``ReactRouterLink`` the render puts underneath it. A sidebar of styled labels
that navigate nowhere would look completely correct in a screenshot.

The second is the gate. ``requires_admin`` renders as a *condition node* —
``{"cond_state": …, "true_value": …, "false_value": …}`` — not as a component
with children, so a walker that only descends into ``children`` (the one the
page tests in ``tests/`` use today) finds no link at all inside a gated item.
That is exactly the shape this file has to read, because "`/` is public"
means an anonymous visitor must not see the ``/admin/*`` entries, and the only
place that fact exists is the condition standing above each of those links.

The third is that a public page carries no authentication check. It is one
handler in a list, it has no visual trace, and putting it back would lock the
dashboard behind a login without changing a single pixel.
"""

from typing import Any

import pytest
import reflex as rx
from reflex.page import DECORATED_PAGES

from mailarc_ui.kit import card_heading, page_header, panel_card, stat_tile
from mailarc_ui.shell import routes
from mailarc_ui.shell.model import NavItem
from mailarc_ui.shell.navigation import NAV_SECTIONS, app_sidebar
from mailarc_ui.shell.templates import mailarc_app, mailarc_full_app, public_page

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""

ADMIN_CONDITION = "is_admin"
"""What the admin gate compiles to: a read of ``UserSession.user.is_admin``."""

PROBE_ROUTE = "/__public_page_probe__"
"""A route no page of this application serves, so nothing can collide."""


def _nav_items() -> list[NavItem]:
    return [item for section in NAV_SECTIONS for item in section.items]


def _link_gates(node: Any, gates: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    """Every route the render points at, mapped to the conditions above it.

    A rendered tree holds two kinds of node. A component is a ``name`` with
    ``props`` and ``children``; a ``rx.cond`` is a ``cond_state`` with a
    ``true_value`` and a ``false_value`` and neither of the other two. Walking
    only the second kind is what makes a gated link visible to this check at
    all, and carrying the condition down with it is what makes "gated" and
    "not gated" tellable apart.
    """
    found: dict[str, tuple[str, ...]] = {}
    if not isinstance(node, dict):
        return found
    if (condition := node.get("cond_state")) is not None:
        found.update(_link_gates(node.get("false_value"), gates))
        found.update(_link_gates(node.get("true_value"), (*gates, str(condition))))
        return found
    for prop in node.get("props", []):
        if isinstance(prop, str) and prop.startswith(LINK_TARGET):
            found[prop.removeprefix(LINK_TARGET).removesuffix('"')] = gates
    for child in node.get("children", []):
        found.update(_link_gates(child, gates))
    return found


@pytest.fixture
def rendered_sidebar() -> dict[str, Any]:
    return app_sidebar().render()


def test_the_sidebar_builds() -> None:
    """A prop appkit_mantine does not have only shows up when it is built."""
    assert isinstance(app_sidebar(), rx.Component)


def test_both_templates_build() -> None:
    """`mailarc_full_app` differs from `mailarc_app` only in how `main` is
    sized, which is precisely the kind of difference a typo survives."""
    body = rx.fragment()
    assert isinstance(mailarc_app(body), rx.Component)
    assert isinstance(mailarc_full_app(rx.fragment()), rx.Component)


def test_every_navigation_item_names_a_declared_route() -> None:
    """`routes.py` is the single source of truth, so a hand-written href in
    the navigation data is a 404 that no other test can see."""
    declared = set(routes.ALL_ROUTES)
    assert declared, "routes.py declares nothing to navigate to"

    for item in _nav_items():
        assert item.href in declared, f"{item.label} points outside routes.py"


def test_the_navigation_covers_every_route_a_person_can_open() -> None:
    """The other direction: a route with no entry is a page nobody can reach
    without typing its path."""
    assert {item.href for item in _nav_items()} == set(routes.ALL_ROUTES)


def test_every_item_renders_a_router_target(rendered_sidebar: dict[str, Any]) -> None:
    """`mn.nav_link` carries no href of its own — read off its render, whose
    props are `label`, `active` and `variant`. The `rx.link` around it is what
    makes the row navigate, and this is the only place that shows."""
    assert set(_link_gates(rendered_sidebar)) == {item.href for item in _nav_items()}


def test_no_navigation_row_renders_a_second_anchor(
    rendered_sidebar: dict[str, Any],
) -> None:
    """The row inside the router link is a ``div``, not another ``<a>``.

    Mantine's ``NavLink`` is polymorphic over ``'a'``, so the default put an
    anchor inside the ``rx.link`` that wraps it — nested interactive elements,
    which React refuses out loud on every page load: "<a> cannot contain a
    nested <a>. This will cause a hydration error." Measured in a browser, not
    reasoned about, and invisible to every other test here because the sidebar
    still renders and still navigates.

    ``component`` reaches the component through ``custom_attrs`` because it is
    not a declared prop of the wrapper, and Reflex folds an undeclared keyword
    into ``css`` — ``mn.nav_link(component="div")`` renders
    ``css:({["component"]:"div"})``, which is a bogus CSS key and no fix at
    all. So the assertion is on the rendered prop, which is the only thing that
    tells the two apart.
    """
    rows = _named(rendered_sidebar, "NavLink")

    assert rows, "the sidebar renders no navigation rows at all"
    for row in rows:
        assert 'component:"div"' in row.get("props", []), (
            "a navigation row renders as Mantine's default anchor, inside the "
            "router anchor that wraps it"
        )


def test_the_dashboard_is_reachable_by_anyone(
    rendered_sidebar: dict[str, Any],
) -> None:
    """`/` is the public page. An entry behind a condition would hide the one
    route an anonymous visitor is meant to have."""
    assert _link_gates(rendered_sidebar)[routes.DASHBOARD] == ()


def test_every_admin_entry_stands_behind_the_admin_gate(
    rendered_sidebar: dict[str, Any],
) -> None:
    """The requirement in one assertion: an anonymous visitor on `/` must not
    be shown a link into the archive's administration."""
    gates = _link_gates(rendered_sidebar)
    admin_routes = [href for href in gates if href.startswith("/admin/")]

    assert admin_routes, "no admin route rendered, so this proves nothing"
    for href in admin_routes:
        assert any(ADMIN_CONDITION in gate for gate in gates[href]), (
            f"{href} renders unconditionally and an anonymous visitor sees it"
        )


def test_a_gated_item_is_gated_by_its_own_declaration() -> None:
    """The gate is data on the item, not a rule about paths that start with
    `/admin/`, so the two have to be checked against each other."""
    by_href = {item.href: item for item in _nav_items()}

    assert by_href[routes.DASHBOARD].admin_only is False
    assert all(
        by_href[href].admin_only for href in by_href if href.startswith("/admin/")
    )


def test_the_kit_primitives_build() -> None:
    """Four components with no state behind them; building them is the whole
    of what can go wrong."""
    assert isinstance(panel_card(rx.fragment()), rx.Component)
    assert isinstance(card_heading("database", "The archive"), rx.Component)
    assert isinstance(card_heading("hard-drive", "Disk"), rx.Component)
    assert isinstance(stat_tile("Messages", 12), rx.Component)
    assert isinstance(page_header("Dashboard", "What is in the archive"), rx.Component)


def test_panel_card_lets_a_caller_override_the_recipe() -> None:
    """Nine call sites share one recipe and one of them needs `on_mount`; a
    primitive that cannot take it would send that card back to a raw
    `mn.card`."""
    card = panel_card(rx.fragment(), padding="xs")
    assert 'padding:"xs"' in card.render()["props"]


def test_a_public_page_is_not_authentication_checked() -> None:
    """The assurance behind "user pages need no sign-in".

    `authenticated_page` puts `LoginState.check_auth` first in every `on_load`
    it builds, and that handler redirects to `/login`. A public page built on
    that decorator would look identical in the source and be unreachable while
    logged out.
    """

    @public_page(route=PROBE_ROUTE, title="Probe")
    def probe() -> rx.Component:
        return rx.fragment()

    try:
        handlers = _handler_names(_page_kwargs(PROBE_ROUTE))
    finally:
        _forget(PROBE_ROUTE)

    assert not any("check_auth" in name for name in handlers), (
        f"a public page is authentication-checked: {sorted(handlers)}"
    )
    assert any("set_is_loading" in name for name in handlers), (
        "without the loading reset the theme keeps the wait cursor on"
    )
    assert isinstance(probe(), rx.Component)


def _page_kwargs(route: str) -> dict[str, Any]:
    """What reflex was told about the page at this route."""
    for pages in DECORATED_PAGES.values():
        for _, kwargs in pages:
            if kwargs.get("route") == route:
                return kwargs
    raise AssertionError(f"no page is registered under {route!r}")


def _handler_names(kwargs: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for handler in kwargs.get("on_load") or []:
        if (fn := getattr(handler, "fn", None)) is not None:
            names.add(fn.__qualname__)
        elif (spec := getattr(handler, "handler", None)) is not None:
            names.add(spec.fn.__qualname__)
    return names


def _forget(route: str) -> None:
    """Unregister the probe page again.

    ``rx.page`` writes into a module-global registry that outlives this test,
    and a page at a route no module owns would follow every later test in the
    same process.
    """
    for module, pages in list(DECORATED_PAGES.items()):
        remaining = [entry for entry in pages if entry[1].get("route") != route]
        if remaining:
            DECORATED_PAGES[module] = remaining
        else:
            del DECORATED_PAGES[module]


def _named(
    node: Any, wanted: str, found: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Every component of one name in a render tree.

    Walks conditions as well as children: each ``/admin/*`` row stands behind a
    ``requires_admin``, which renders as a ``cond_state`` with a ``true_value``
    and a ``false_value`` and no ``children`` at all.
    """
    found = [] if found is None else found
    if not isinstance(node, dict):
        return found
    if node.get("name") == wanted:
        found.append(node)
    for child in node.get("children", []):
        _named(child, wanted, found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _named(subtree, wanted, found)
    return found
