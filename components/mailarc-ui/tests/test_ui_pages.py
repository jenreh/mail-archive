"""Every page of the archive: it builds, it answers, it primes itself, it is framed.

One module for six pages, replacing the four ``tests/test_mail_*_page.py``
files that each asked the same questions of one page and copied the four
helpers between them. The questions are the ones a page can get wrong in
silence: a route nobody links to, an ``on_load`` nobody fires, and a layout
nobody applied.

There used to be a fifth question — the gate — and it is gone with the
sign-in. ``admin_only`` was a render-time ``rx.cond`` over a page that appkit
served to whoever asked anyway; the archive is a desktop application now, with
one person's mail on one person's machine, and there is nothing left to gate
against. ``PageSpec`` lost the field rather than defaulting it to ``False``,
because a table full of ``admin_only=False`` reads as a decision that was made
rather than one that no longer exists.
"""

from collections.abc import Callable
from typing import Any

import pytest
import reflex as rx
from pydantic import BaseModel, ConfigDict
from reflex.page import DECORATED_PAGES

from mailarc_analytics.semantic import SETTINGS_PAGE, dimension_mismatch
from mailarc_ui.embedder import embedder_panel
from mailarc_ui.kit import PAGE_INSET
from mailarc_ui.pages import (
    accounts,
    dashboard,
    embedder,
    insights,
    search,
    status,
)
from mailarc_ui.shell import routes

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""


class PageSpec(BaseModel):
    """What one page of this application promises.

    A frozen model rather than a tuple, because four positional fields read as
    four anonymous strings at every call site.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    page: Callable[[], Any]
    route: str
    title: str
    primes: tuple[str, ...] = ()
    """Handlers the page's ``on_load`` has to fire, by qualified name."""

    capped: bool = False
    """Whether this page holds a maximum width and centres itself in the window.

    One page does — the dashboard, whose two charts stop being readable rather
    than merely get wider — and it says so here so that every other page can
    be asserted against the opposite. A page that quietly grows an ``mx="auto"``
    is a page that reads as narrower than the one before it in the rail, and
    nothing else in this file would notice.
    """


PAGES: tuple[PageSpec, ...] = (
    PageSpec(
        name="search",
        page=search.search_page,
        route=routes.SEARCH,
        title="Search",
        primes=("MailSearchState.load",),
    ),
    PageSpec(
        name="dashboard",
        page=dashboard.dashboard_page,
        route=routes.DASHBOARD,
        title="Dashboard",
        primes=("DashboardState.load",),
        capped=True,
    ),
    PageSpec(
        name="insights",
        page=insights.insights_page,
        route=routes.INSIGHTS,
        title="Insights",
        primes=("AnalyticsInsightsState.load",),
    ),
    PageSpec(
        name="accounts",
        page=accounts.accounts_page,
        route=routes.ACCOUNTS,
        title="Mail accounts",
        primes=("MailAccountState.load", "ImportJobState.refresh"),
    ),
    PageSpec(
        name="embedder",
        page=embedder.embedder_page,
        route=routes.EMBEDDER,
        title="Embedder",
        primes=("EmbedderSettingsState.load",),
    ),
    PageSpec(
        name="status",
        page=status.graph_status_page,
        route=routes.GRAPH_STATUS,
        title="Graph status",
        primes=("GraphStatusState.start_polling",),
    ),
)
"""Every page this application registers, and what it claims to be.

In the rail's own order: the search a person arrives at, then the two other
pages the menu offers, then the three the administration popover holds. Every
one of them primes itself, the search included — it opens on the newest
messages, so a page that stopped firing ``load`` would show an empty archive
and say nothing about why.
"""

IDS = [spec.name for spec in PAGES]


def _link_targets(node: Any, found: list[str] | None = None) -> list[str]:
    """Every route a rendered component points at.

    Read off the render rather than off the source, because the render is what
    the browser is handed — a constant nobody links to would prove nothing.
    Condition nodes are walked as well as children: a rendered ``rx.cond`` is a
    ``cond_state`` with a ``true_value`` and a ``false_value`` and no
    ``children`` at all, and a page's body is full of them.
    """
    found = [] if found is None else found
    if not isinstance(node, dict):
        return found
    for prop in node.get("props", []):
        if isinstance(prop, str) and prop.startswith(LINK_TARGET):
            found.append(prop.removeprefix(LINK_TARGET).removesuffix('"'))
    for child in node.get("children", []):
        _link_targets(child, found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _link_targets(subtree, found)
    return found


def _rendered_props(node: Any, found: list[str] | None = None) -> list[str]:
    """Every prop a rendered tree carries, as the strings the browser is handed.

    Walked the way :func:`_link_targets` walks it, and for the same reason: a
    page's body is mostly ``rx.cond``, which renders as a node with branches
    rather than with children.
    """
    found = [] if found is None else found
    if not isinstance(node, dict):
        return found
    found.extend(prop for prop in node.get("props", []) if isinstance(prop, str))
    for child in node.get("children", []):
        _rendered_props(child, found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _rendered_props(subtree, found)
    return found


def _page_kwargs(route: str) -> dict[str, Any]:
    """What reflex was told about the page at this route."""
    for pages in DECORATED_PAGES.values():
        for _, kwargs in pages:
            if kwargs.get("route") == route:
                return kwargs
    raise AssertionError(f"no page is registered under {route!r}")


def _handler_names(kwargs: dict[str, Any]) -> set[str]:
    """The handlers an ``on_load`` chain fires, by qualified name.

    Two shapes, because ``on_load`` holds both: an ``EventHandler`` carries its
    function directly, and an ``EventSpec`` — what
    ``LoadingState.set_is_loading(False)`` evaluates to — carries a handler
    that carries it.
    """
    names: set[str] = set()
    for handler in kwargs.get("on_load") or []:
        if (fn := getattr(handler, "fn", None)) is not None:
            names.add(fn.__qualname__)
        elif (spec := getattr(handler, "handler", None)) is not None:
            names.add(spec.fn.__qualname__)
    return names


def _unmount_handlers(chain: Any) -> set[str]:
    """The handler names an ``on_unmount`` chain will fire.

    Reached with ``getattr`` rather than by attribute, the way
    :func:`_handler_names` reaches an ``on_load`` handler and for the same
    reason: Reflex types an event trigger as a union wide enough that ``ty``
    cannot see ``EventChain.events`` on it, and a chain of ``ty: ignore``
    comments over one expression says less than this does.
    """
    names: set[str] = set()
    for event in getattr(chain, "events", ()):
        fn = getattr(getattr(event, "handler", None), "fn", None)
        if fn is not None:
            names.add(fn.__qualname__)
    return names


@pytest.mark.parametrize("spec", PAGES, ids=IDS)
class TestEveryPage:
    """The four questions, asked of each page in turn."""

    def test_it_builds(self, spec: PageSpec) -> None:
        """A prop appkit_mantine does not have only shows up when it is built."""
        assert isinstance(spec.page(), rx.Component)

    def test_it_answers_at_its_route(self, spec: PageSpec) -> None:
        """Importing the module is what registers it; app.py does the importing."""
        assert _page_kwargs(spec.route)["title"] == spec.title

    def test_it_is_primed_on_load(self, spec: PageSpec) -> None:
        """A page that never asks opens blank and says nothing about why."""
        names = _handler_names(_page_kwargs(spec.route))
        for handler in spec.primes:
            assert handler in names, f"{spec.route} never fires {handler}"

    def test_it_clears_the_wait_cursor(self, spec: PageSpec) -> None:
        """``theme_wrapper`` shows a wait cursor while ``LoadingState`` is set.

        Every page's ``on_load`` ends with the reset, and a page that lost it
        renders perfectly and keeps the cursor for as long as the visitor
        stays. There is no other symptom.
        """
        assert "LoadingState.set_is_loading" in _handler_names(_page_kwargs(spec.route))

    def test_it_renders_under_the_archive_shell(self, spec: PageSpec) -> None:
        """The rail has to be on the page, and only the template puts it there.

        A page that forgot its ``template`` argument renders correctly, answers
        at its route and has no navigation at all — which no other assertion in
        this file would notice. ``/`` is the first entry in the rail, so it is
        the one that proves the rail is there.
        """
        assert routes.SEARCH in _link_targets(spec.page().render())

    def test_it_fills_the_window(self, spec: PageSpec) -> None:
        """Only the dashboard centres itself; every other page takes the width.

        Three pages used to cap at 900, 1200 and 1280 and centre what was
        left, which put a second margin beside the rail's — one that changed
        from page to page — and left a four-column table drawn down the middle
        of a 1300px window. ``mx:"auto"`` is the whole of that behaviour, so
        its absence is the whole of the assertion.
        """
        centred = 'mx:"auto"' in _rendered_props(spec.page().render())

        assert centred == spec.capped

    def test_it_keeps_the_rail_gap(self, spec: PageSpec) -> None:
        """A page pads its rail side by twelve pixels, not by twenty-four.

        Padding all four sides equally puts forty pixels between an icon and
        the content beside it — the rail's own sixteen and a full page margin —
        and the column reads as having drifted away from the navigation it
        belongs to. Read off the render, because a page that imported the
        constant and passed ``PAGE_PADDING`` anyway would still pass a test
        that only looked at the import.
        """
        assert f'p:"{PAGE_INSET}"' in _rendered_props(spec.page().render())


def test_every_declared_route_has_a_page() -> None:
    """``routes.py`` is the source of truth, so a constant no page answers at is
    a rail entry that 404s.

    Equality and not containment, in both directions: a page at a path the
    table does not name is a page the rail cannot reach.
    """
    covered = {spec.route for spec in PAGES}
    assert covered == set(routes.ALL_ROUTES)


def test_the_embedder_panel_stops_following_a_rebuild_when_it_goes_away() -> None:
    """``stop_polling`` needs a caller, and having one is not testable from the
    state.

    Asserted at the page level rather than in the state tests because the
    failure this catches is *silence*: a handler that clears the flag
    correctly, a test that proves it does, and nothing anywhere calling it.
    That is not hypothetical — the insights panel shipped in exactly that
    shape, and a user who navigated away mid-rebuild left a background task
    hitting the database every two seconds for the life of the session, one
    per abandoned page. The rebuild card on this page starts the same kind of
    poll.
    """
    chain = embedder_panel().event_triggers.get("on_unmount")

    assert chain is not None, "the panel starts a poll and never stops it"
    assert "EmbedderSettingsState.stop_polling" in _unmount_handlers(chain)


def test_every_remedy_that_names_the_embedder_page_names_its_route() -> None:
    """``mailarc-analytics`` writes the sentence; ``mailarc-ui`` owns the route.

    The analytics component may not import the interface, so the route is a
    literal in ``mailarc_analytics.semantic.errors`` and the two can drift —
    after which a message that sends its reader to a page sends them to a 404.
    That is a worse outcome than the sentence not naming a page at all, so the
    drift is pinned here, in a test module that legitimately sees both sides.

    Asserted through ``dimension_mismatch`` because that is now the remedy
    that names the page. The short states — no embedder, no index — say what
    is off and leave the fix to the page itself, so there is nothing in them
    left to drift.
    """
    assert SETTINGS_PAGE == routes.EMBEDDER

    advice = dimension_mismatch(
        index=768, model="text-embedding-3-small", produced=1536
    )

    assert routes.EMBEDDER in advice
