"""The rail: where it links, what it is called, and what a page is not.

Four things are worth pinning here and none of them can be settled by reading
the source.

The first is that a rail entry reaches the browser as a *router* link. The
square itself is an ``mn.box`` — a ``div`` with a class and a ``data-active``
attribute, no ``href`` anywhere — so every entry is wrapped in an ``rx.link``,
and the proof is the ``to:"…"`` prop on the ``ReactRouterLink`` the render puts
underneath it. A rail of icons that navigate nowhere would look completely
correct in a screenshot.

The second is that the entries name themselves. The rail is 76px wide and
shows no text, so an item's ``label`` reaches a person only through the
tooltip beside it — a missing one is an icon nobody can identify.

The third is that the administration is a second run of icons rather than a
popover, and that the two sections are therefore indistinguishable to a
renderer: every entry of both is a router link with a tooltip. The popover it
replaced is worth naming because its absence is what these tests now pin — one
``mn.menu`` behind a settings icon, whose rows navigated with ``rx.redirect``
because a link inside a menu item is an anchor inside a button. That is the
same class of bug the previous sidebar hit with ``mn.nav_link`` ("<a> cannot
contain a nested <a>. This will cause a hydration error"), so the guard against
it is kept whatever the rail is made of: **no rail entry may render a second
anchor**.

The fourth is that a page carries no authentication check. It is one handler
in a list, it has no visual trace, and putting it back would lock the whole
archive behind a login without changing a single pixel.
"""

import re
from typing import Any

import pytest
import reflex as rx
from reflex.page import DECORATED_PAGES

from mailarc_ui.kit import card_heading, column_card, panel_card, stat_tile
from mailarc_ui.shell import routes
from mailarc_ui.shell.model import NavItem
from mailarc_ui.shell.navigation import NAVBAR_WIDTH, RAIL_SECTIONS, app_sidebar
from mailarc_ui.shell.templates import mailarc_app, mailarc_full_app, public_page

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""

REDIRECT_PATH = re.compile(r'"_redirect".*?\["path"\] : "([^"]+)"')
"""What ``rx.redirect`` compiles to inside a rendered ``onClick``.

Read with a pattern rather than by equality because the rest of the expression
is Reflex's own event plumbing — ``addEvents``, an argument spread, three
``false`` flags — and asserting on all of it would pin a spelling this test
has no opinion about. The path is the part that decides where a row goes.
"""

PROBE_ROUTE = "/__public_page_probe__"
"""A route no page of this application serves, so nothing can collide."""

MENU, ADMINISTRATION = RAIL_SECTIONS


def _nav_items() -> list[NavItem]:
    return [item for section in RAIL_SECTIONS for item in section.items]


def _props(node: Any, found: list[str] | None = None) -> list[str]:
    """Every rendered prop in a tree, conditions walked as well as children."""
    found = [] if found is None else found
    if not isinstance(node, dict):
        return found
    found.extend(prop for prop in node.get("props", []) if isinstance(prop, str))
    for child in node.get("children", []):
        _props(child, found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _props(subtree, found)
    return found


def _link_targets(node: Any) -> set[str]:
    """Every route the render points a router link at."""
    return {
        prop.removeprefix(LINK_TARGET).removesuffix('"')
        for prop in _props(node)
        if prop.startswith(LINK_TARGET)
    }


def _named(
    node: Any, wanted: str, found: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Every component of one name in a render tree, conditions included."""
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


@pytest.fixture
def rendered_rail() -> dict[str, Any]:
    return app_sidebar().render()


def test_the_rail_builds() -> None:
    """A prop appkit_mantine does not have only shows up when it is built."""
    assert isinstance(app_sidebar(), rx.Component)


def test_the_rail_is_a_rail_and_not_a_sidebar() -> None:
    """76px is the design, and it is the shell that reserves it.

    Asserted because the number lives in two places that cannot see each other
    — this constant and the stylesheet's `.ma-rail` padding — and a rail wide
    enough for labels is the old sidebar back again.
    """
    assert NAVBAR_WIDTH == 76


def test_both_templates_build() -> None:
    """`mailarc_full_app` differs from `mailarc_app` only in how `main` is
    sized, which is precisely the kind of difference a typo survives."""
    body = rx.fragment()
    assert isinstance(mailarc_app(body), rx.Component)
    assert isinstance(mailarc_full_app(rx.fragment()), rx.Component)


def test_every_rail_entry_names_a_declared_route() -> None:
    """`routes.py` is the single source of truth, so a hand-written href in
    the navigation data is a 404 that no other test can see."""
    declared = set(routes.ALL_ROUTES)
    assert declared, "routes.py declares nothing to navigate to"

    for item in _nav_items():
        assert item.href in declared, f"{item.label} points outside routes.py"


def test_the_rail_covers_every_route_a_person_can_open() -> None:
    """The other direction: a route with no entry is a page nobody can reach
    without typing its path."""
    assert {item.href for item in _nav_items()} == set(routes.ALL_ROUTES)


def test_the_menu_is_the_four_pages_a_person_works_in() -> None:
    """The redesign's own decision plus the explorer, in one assertion: search
    first, the dashboard demoted to the rail, insights out from under
    `/admin/`, and the graph beside insights.

    Four rather than three since the explorer arrived, which is a deliberate
    change to a pin (spec R9): `/graph` reads the same derived layer insights
    tabulates, so it belongs in the menu a person works in rather than in the
    administration popover.
    """
    assert [item.href for item in MENU.items] == [
        routes.SEARCH,
        routes.DASHBOARD,
        routes.INSIGHTS,
        routes.GRAPH,
    ]
    assert MENU.label == "Menu"


def test_the_administration_is_the_three_pages_an_operator_maintains() -> None:
    """And they are all of `/admin/`, so nothing is reachable only by typing."""
    assert {item.href for item in ADMINISTRATION.items} == {
        route for route in routes.ALL_ROUTES if route.startswith("/admin/")
    }
    assert ADMINISTRATION.label == "Admin"


def test_every_menu_entry_renders_a_router_target(
    rendered_rail: dict[str, Any],
) -> None:
    """The rail's square carries no href of its own — it is an `mn.box`. The
    `rx.link` around it is what makes it navigate, and this is the only place
    that shows."""
    assert _link_targets(rendered_rail) == {item.href for item in _nav_items()}


def test_every_menu_entry_names_itself_in_a_tooltip(
    rendered_rail: dict[str, Any],
) -> None:
    """At 76px the tooltip is not decoration, it is the label.

    Asserted on the rendered prop rather than on the data, because an item
    whose tooltip was dropped still renders, still navigates and still lights
    up when it is current — it is simply an icon nobody can name.
    """
    labelled = {
        prop.removeprefix('label:"').removesuffix('"')
        for tooltip in _named(rendered_rail, "Tooltip")
        for prop in tooltip.get("props", [])
        if isinstance(prop, str) and prop.startswith('label:"')
    }

    assert {item.label for item in _nav_items()} <= labelled


def test_the_search_entry_accepts_both_spellings_of_the_index(
    rendered_rail: dict[str, Any],
) -> None:
    """Reflex serves the index under `/` and `/index`, and which one the router
    reports depends on how the page was reached. An item keyed on one of them
    goes dark on a reload."""
    keyed = [prop for prop in _props(rendered_rail) if prop.startswith('"data-active"')]

    assert any('"/index"' in prop and '"/"' in prop for prop in keyed), (
        "the search entry is keyed on one spelling of the index only"
    )


def _nests_an_anchor(node: Any, inside: bool = False) -> bool:
    """Whether any `ReactRouterLink` sits inside another one."""
    if not isinstance(node, dict):
        return False
    here = node.get("name") == "ReactRouterLink"
    if here and inside:
        return True
    return any(
        _nests_an_anchor(child, inside or here)
        for child in (node.get("children") or [])
    )


def test_no_rail_entry_renders_a_second_anchor(rendered_rail: dict[str, Any]) -> None:
    """The replacement for the `mn.nav_link` guard the sidebar needed.

    React refuses nested interactive elements out loud on every page load —
    "<a> cannot contain a nested <a>. This will cause a hydration error" —
    which was measured in a browser rather than reasoned about, and is
    invisible to every other test here because the navigation still renders
    and still navigates.

    The property is **one anchor per entry and none inside another**, which is
    what `mn.nav_link` broke by rendering an anchor of its own inside the
    `rx.link` around it. It is deliberately not a count of the *menu* section:
    that pinned a layout — the administration behind a popover — rather than
    the hydration bug, and went red the moment the rail grew a section or a
    page. Every rail square is an `mn.box`, a plain div, so the `rx.link`
    around it stays the only anchor it has.
    """
    anchors = _named(rendered_rail, "ReactRouterLink")

    assert not _nests_an_anchor(rendered_rail), (
        "a rail entry renders an anchor inside another anchor, which React "
        "refuses to hydrate"
    )
    assert len(anchors) == len(_nav_items()), (
        f"the rail renders {len(anchors)} router anchors for "
        f"{len(_nav_items())} entries"
    )
    for anchor in anchors:
        assert not _named(anchor, "ReactRouterLink")[1:], (
            "a rail entry renders an anchor inside its own anchor"
        )
        assert not _named(anchor, "Menu.Item"), (
            "a menu item renders inside a router link"
        )


def test_the_kit_primitives_build() -> None:
    """Four components with no state behind them; building them is the whole
    of what can go wrong."""
    assert isinstance(panel_card(rx.fragment()), rx.Component)
    assert isinstance(card_heading("database", "The archive"), rx.Component)
    assert isinstance(card_heading("hard-drive", "Disk"), rx.Component)
    assert isinstance(stat_tile("Messages", 12), rx.Component)
    assert isinstance(column_card(rx.fragment()), rx.Component)


def test_panel_card_lets_a_caller_override_the_recipe() -> None:
    """Nine call sites share one recipe and one of them needs `on_mount`; a
    primitive that cannot take it would send that card back to a raw
    `mn.card`."""
    card = panel_card(rx.fragment(), padding="xs")
    assert 'padding:"xs"' in card.render()["props"]


def test_a_page_is_not_authentication_checked() -> None:
    """The assurance behind "the archive has no sign-in".

    appkit's decorators put `LoginState.check_auth` first in every `on_load`
    they build, and that handler redirects to a sign-in page this application
    no longer serves. A page built on one of them would look identical in the
    source and be unreachable.
    """

    @public_page(route=PROBE_ROUTE, title="Probe")
    def probe() -> rx.Component:
        return rx.fragment()

    try:
        handlers = _handler_names(_page_kwargs(PROBE_ROUTE))
    finally:
        _forget(PROBE_ROUTE)

    assert not any("check_auth" in name for name in handlers), (
        f"a page is authentication-checked: {sorted(handlers)}"
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
