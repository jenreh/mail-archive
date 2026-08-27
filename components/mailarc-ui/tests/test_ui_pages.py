"""Every page of the archive: it builds, it answers, it primes itself, it is gated.

One module for seven pages, replacing the four ``tests/test_mail_*_page.py``
files that each asked the same five questions of one page and copied the four
helpers between them. The questions did not change — a page is wired up in five
places and four of them are silent when they are wrong: a route nobody links
to, an ``on_load`` nobody fires, a gate nobody set, and a layout nobody
applied.

What did change is where the answer lives. The pages moved into
``mailarc_ui.pages`` and the route table into ``mailarc_ui.shell.routes``, so
the questions are asked once against a table rather than seven times against
seven modules — and a page added without an entry here is a page nothing
checks.

The helpers those files invented are kept, deliberately: ``_page_kwargs``,
``_handler_names`` and ``_unmount_handlers`` read Reflex's own registries, and
``_gate_of`` reads ``admin_only`` out of the decorator's closure because there
is nowhere else to read it. ``_link_targets`` had to learn one thing on the way
— to walk a condition node. The pages are now behind ``rx.cond(admin_only, …)``
and the sidebar's own entries behind ``requires_admin``, and a walker that
descends only into ``children`` sees an empty tree for both.
"""

from collections.abc import Callable
from typing import Any

import pytest
import reflex as rx
from pydantic import BaseModel, ConfigDict
from reflex.page import DECORATED_PAGES

from mailarc_analytics.semantic import NO_EMBEDDER, SETTINGS_PAGE
from mailarc_ui.embedder import embedder_panel
from mailarc_ui.pages import (
    accounts,
    auth,
    dashboard,
    embedder,
    insights,
    profile,
    review,
    status,
    users,
)
from mailarc_ui.shell import routes

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""


class PageSpec(BaseModel):
    """What one page of this application promises.

    A frozen model rather than a tuple, because five positional fields read as
    five anonymous strings at every call site and the one that matters —
    ``admin_only`` — is the one a reader would have to count to find.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    page: Callable[[], Any]
    route: str
    title: str
    admin_only: bool
    primes: tuple[str, ...] = ()
    """Handlers the page's ``on_load`` has to fire, by qualified name."""

    public: bool = False
    """Built with ``public_page`` rather than with one of appkit's decorators.

    Which means it carries no ``admin_only`` at all — not ``admin_only=False``.
    :func:`_gate_of` reads that name out of the decorator's closure, so a page
    that has no such decorator is the one case where the absence *is* the
    assertion: ``public_page`` has no way to gate a page and is not going to
    grow one.
    """


PAGES: tuple[PageSpec, ...] = (
    PageSpec(
        name="dashboard",
        page=dashboard.dashboard_page,
        route=routes.DASHBOARD,
        title="Dashboard",
        admin_only=False,
        public=True,
        primes=("DashboardState.load",),
    ),
    PageSpec(
        name="status",
        page=status.graph_status_page,
        route=routes.GRAPH_STATUS,
        title="Graph status",
        admin_only=True,
        primes=("GraphStatusState.start_polling",),
    ),
    PageSpec(
        name="accounts",
        page=accounts.accounts_page,
        route=routes.ACCOUNTS,
        title="Mail accounts",
        admin_only=True,
        primes=("MailAccountState.load", "ImportJobState.refresh"),
    ),
    PageSpec(
        name="review",
        page=review.review_page,
        route=routes.REVIEW,
        title="Review",
        admin_only=True,
        primes=("MessageReviewState.load",),
    ),
    PageSpec(
        name="insights",
        page=insights.insights_page,
        route=routes.INSIGHTS,
        title="Insights",
        admin_only=True,
        primes=("AnalyticsInsightsState.load",),
    ),
    PageSpec(
        name="embedder",
        page=embedder.embedder_page,
        route=routes.EMBEDDER,
        title="Embedder",
        admin_only=True,
        primes=("EmbedderSettingsState.load",),
    ),
    PageSpec(
        name="users",
        page=users.users_page,
        route=routes.USERS,
        title="Benutzerverwaltung",
        admin_only=True,
        primes=("UserState.set_available_roles",),
    ),
    PageSpec(
        name="profile",
        page=profile.profile_page,
        route=routes.PROFILE,
        title="Profil",
        admin_only=False,
    ),
)
"""Every page this application registers, and what it claims to be.

``/`` is first because it is the address a visitor arrives at, and it is the
one entry that is neither administration nor somebody's own page: the dashboard
is public, which is why it carries ``public=True`` and why the gate assertion
below asks a different question of it than of the seven under it.
"""

IDS = [spec.name for spec in PAGES]


def _link_targets(node: Any, found: list[str] | None = None) -> list[str]:
    """Every route a rendered component points at.

    Read off the render rather than off the source, because the render is what
    the browser is handed — a constant nobody links to would prove nothing.

    Condition nodes are walked as well as children, which the version in the
    files this replaces did not do. A rendered ``rx.cond`` is a ``cond_state``
    with a ``true_value`` and a ``false_value`` and no ``children`` at all, and
    both of the things this needs to see are behind one: ``authenticated_page``
    wraps every page in ``rx.cond(admin_only, …)``, and the sidebar puts each
    ``/admin/*`` entry behind ``requires_admin``.
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
    function directly, and an ``EventSpec`` — what ``UserState.set_available_roles(…)``
    and ``LoadingState.set_is_loading(False)`` evaluate to — carries a handler
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


def _gate_of(page: Any) -> bool:
    """Whether the decorator was told to admin-gate this page.

    Read off the decorator's closure because there is nowhere else to read it:
    ``admin_only`` is a plain bool, so ``rx.cond`` folds it away at build time
    and the rendered tree of a gated page is identical to an ungated one. A
    security control with no runtime trace still deserves a test.

    ``authenticated_page`` closes over the same name ``authenticated`` did —
    ``test_the_gate_is_read_from_a_closure_that_can_say_false`` is what proves
    that rather than assuming it, because a rename in appkit would otherwise
    turn every gate assertion below into an error nobody reads as one.
    """
    closure = dict(zip(page.__code__.co_freevars, page.__closure__ or (), strict=True))
    cell = closure.get("admin_only")
    if cell is None:
        raise AssertionError("this page was not built by an appkit page decorator")
    return bool(cell.cell_contents)


@pytest.mark.parametrize("spec", PAGES, ids=IDS)
class TestEveryPage:
    """The five questions, asked of each page in turn."""

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

    def test_its_gate_is_what_it_claims(self, spec: PageSpec) -> None:
        """The archive is every mailbox of the installation — everybody's mail.

        Asserted here but not relied on: ``admin_only`` expands to a
        render-time ``rx.cond`` while appkit runs the whole ``on_load`` chain
        whatever ``check_auth`` returned. What actually refuses is each
        state's own ``_may_*`` check. This is the cosmetic half.

        A public page has no gate to read, and that is the assertion for it:
        ``public_page`` closes over no ``admin_only``, so a dashboard that had
        quietly been rebuilt on ``authenticated_page`` — which would put
        ``check_auth`` in front of its ``on_load`` and redirect every visitor
        to ``/login`` — would stop raising here.
        """
        if spec.public:
            with pytest.raises(AssertionError):
                _gate_of(spec.page)
            return
        assert _gate_of(spec.page) is spec.admin_only

    def test_it_renders_under_the_archive_shell(self, spec: PageSpec) -> None:
        """The sidebar has to be on the page, and only the template puts it there.

        ``authenticated_page`` falls back to a bare ``mn.app_shell`` with a
        ``main`` and nothing else when ``template`` is omitted, so a page that
        forgot the argument renders correctly, answers at its route and has no
        navigation at all — which no other assertion in this file would notice.
        ``/`` is the one link in the sidebar that stands behind no condition,
        so it is the one that proves the sidebar is there.
        """
        assert routes.DASHBOARD in _link_targets(spec.page().render())


def test_every_declared_route_has_a_page() -> None:
    """``routes.py`` is the source of truth, so a constant no page answers at is
    a sidebar entry that 404s.

    Equality and not containment, in both directions: a page at a path the
    table does not name is a page the sidebar cannot reach.
    """
    covered = {spec.route for spec in PAGES}
    assert covered == set(routes.ALL_ROUTES)


def test_the_gate_is_read_from_a_closure_that_can_say_false() -> None:
    """Otherwise every gate assertion above would pass on any page at all.

    The negative control the file this replaces had, and it is not decoration:
    ``_gate_of`` reads a free variable by name, and a decorator that stopped
    closing over ``admin_only`` would raise — but one that closed over a
    *different* value, or a table that listed every page as gated, would sail
    through. ``/profile`` is the page that is deliberately not gated, so it is
    the one that keeps the reading honest.
    """
    assert _gate_of(profile.profile_page) is False
    assert _gate_of(users.users_page) is True


def test_the_authentication_pages_are_registered_by_one_call() -> None:
    """``app/app.py`` holds no page registration of its own any more.

    appkit's login and password-reset pages are created by calling a factory,
    not by importing a module, so they are the one part of the interface that
    could not simply move — :func:`register_auth_pages` is what moved it.
    Calling it twice is what a reload does, and Reflex would answer with two
    pages at ``/login``.
    """
    auth.register_auth_pages()
    auth.register_auth_pages()

    for route in auth.AUTH_ROUTES:
        registered = [
            kwargs
            for pages in DECORATED_PAGES.values()
            for _, kwargs in pages
            if kwargs.get("route") == route
        ]
        assert len(registered) == 1, f"{route} is registered {len(registered)} times"


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
    after which every embedder-off message in the archive, the MCP tool's
    included, sends its reader to a 404. That is a worse outcome than the
    sentence not naming a page at all, so the drift is pinned here, in a test
    module that legitimately sees both sides.
    """
    assert SETTINGS_PAGE == routes.EMBEDDER
    assert routes.EMBEDDER in NO_EMBEDDER
