"""The embedder page: it builds, it is reachable, it primes itself, it is gated.

The same five questions ``test_mail_insights_page.py`` asks, and for the same
reason: a page is wired up in five places and four of them are silent when they
are wrong — a route nobody links to, an ``on_load`` nobody fires, a gate nobody
set, and a service the composition root never published.

The fifth question is sharper here than on the other three pages. This is the
one route that *writes* configuration for the whole installation and holds the
credential the embedder is used with, so the boot probe checks that
``SemanticControl`` is actually in the registry: without it the form loads, says
its developer error and can save nothing, which is a page that looks broken for
a reason no user could guess.

The helpers below are copied from the other page tests rather than shared. They
read Reflex's own module-level registry, which a test that imported them from a
sibling test module would have to import that module's application state along
with — and each of these files is deliberately readable on its own.
"""

import subprocess
import sys
from typing import Any

import reflex as rx
from reflex.page import DECORATED_PAGES

from app.components.navbar import app_navbar
from app.pages.mail_embedder import ROUTE, mail_embedder_page
from mailarc_ui.embedder import embedder_panel

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""

BOOT_PROBE = f"""
import app.app
from appkit_commons.registry import service_registry
from mailarc_analytics.semantic import SemanticControl
from reflex.page import DECORATED_PAGES

routes = {{kwargs.get("route") for pages in DECORATED_PAGES.values() for _, kwargs in pages}}
print({ROUTE!r} in routes, service_registry().has(SemanticControl), sep=",")
"""
"""What starting the application has to leave behind, asked from outside."""


def _link_targets(node: dict[str, Any]) -> list[str]:
    targets = [
        prop.removeprefix(LINK_TARGET).removesuffix('"')
        for prop in node.get("props", [])
        if prop.startswith(LINK_TARGET)
    ]
    for child in node.get("children", []):
        targets.extend(_link_targets(child))
    return targets


def _page_kwargs(route: str) -> dict[str, Any]:
    for pages in DECORATED_PAGES.values():
        for _, kwargs in pages:
            if kwargs.get("route") == route:
                return kwargs
    raise AssertionError(f"no page is registered under {route!r}")


def _handler_names(kwargs: dict[str, Any]) -> set[str]:
    return {
        handler.fn.__qualname__
        for handler in kwargs.get("on_load") or []
        if getattr(handler, "fn", None) is not None
    }


def _unmount_handlers(chain: Any) -> set[str]:
    """The handler names an ``on_unmount`` chain will fire.

    Reached with ``getattr`` rather than by attribute, the way
    :func:`_handler_names` reaches an ``on_load`` handler and for the same
    reason: Reflex types an event trigger as a union wide enough that ``ty``
    cannot see ``EventChain.events`` on it, and a chain of ``ty: ignore``
    comments over one expression says less than this does.
    """
    names = set()
    for event in getattr(chain, "events", ()):
        fn = getattr(getattr(event, "handler", None), "fn", None)
        if fn is not None:
            names.add(fn.__qualname__)
    return names


def _gate_of(page: Any) -> bool:
    """Whether `@authenticated` was told to admin-gate this page; see
    ``tests/test_mail_accounts_page.py`` for why it is read off the closure."""
    closure = dict(zip(page.__code__.co_freevars, page.__closure__ or (), strict=True))
    cell = closure.get("admin_only")
    if cell is None:
        raise AssertionError("this page was not built by @authenticated")
    return bool(cell.cell_contents)


def test_the_page_builds() -> None:
    """A prop appkit_mantine does not have only shows up when it is built."""
    assert isinstance(mail_embedder_page(), rx.Component)  # ty: ignore[call-non-callable]


def test_the_navbar_links_to_the_page() -> None:
    """The search panel tells a user semantic search is off; this is where they
    go about it, and a page nothing links to is a page nobody finds."""
    assert ROUTE in _link_targets(app_navbar().render())


def test_the_page_answers_at_that_route() -> None:
    assert _page_kwargs(ROUTE)["title"] == "Embedder"


def test_the_form_is_primed_on_load() -> None:
    """The form opens on what is in force, so a page that never asks would show
    an empty form and save that emptiness over a working configuration."""
    assert "EmbedderSettingsState.load" in _handler_names(_page_kwargs(ROUTE))


def test_the_page_is_admin_only() -> None:
    """The cosmetic half of a gate whose real half is per-handler.

    ``admin_only`` expands to a render-time ``rx.cond``; appkit builds the
    ``on_load`` chain separately and Reflex runs all of it whatever
    ``check_auth`` returned, and every handler on this state is addressable by
    name over the socket. What actually refuses is
    ``EmbedderSettingsState._may_configure``, and
    ``TestEveryHandlerIsGated`` in ``test_ui_embedder_state.py`` is where that
    is proved.
    """
    assert _gate_of(mail_embedder_page) is True


def test_the_panel_stops_following_a_rebuild_when_it_goes_away() -> None:
    """``stop_polling`` needs a caller, and having one is not testable from the
    state.

    Asserted here rather than in the state tests because the failure this
    catches is *silence*: a handler that clears the flag correctly, a test that
    proves it does, and nothing anywhere calling it. That is not hypothetical —
    the insights panel shipped in exactly that shape, and a user who navigated
    away mid-rebuild left a background task hitting the database every two
    seconds for the life of the session, one per abandoned page. The rebuild
    card here starts the same kind of poll.
    """
    chain = embedder_panel().event_triggers.get("on_unmount")

    assert chain is not None, "the panel starts a poll and never stops it"
    assert "EmbedderSettingsState.stop_polling" in _unmount_handlers(chain)


def test_starting_the_application_wires_the_page_up() -> None:
    """``app/app.py`` imports the page and publishes the control, and neither
    line shows up in a test that imports the page itself. In its own
    interpreter, so the whole application is not registered for every other
    test in this process."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", BOOT_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"importing app.app failed:\n{result.stderr}"
    answer = result.stdout.strip().splitlines()[-1]

    assert answer == "True,True", (
        f"the application started without wiring the page up:\n{result.stdout}"
    )


def test_every_remedy_that_names_this_page_names_this_route() -> None:
    """``mailarc-analytics`` writes the sentence; ``app`` owns the route.

    A component may not import ``app`` (§6), so the route is a literal in
    ``mailarc_analytics.semantic.errors`` and the two can drift — after which
    every embedder-off message in the archive, the MCP tool's included, sends
    its reader to a 404. That is a worse outcome than the sentence not naming a
    page at all, so the drift is pinned here, in the one module that legitimately
    sees both sides.
    """
    from mailarc_analytics.semantic import NO_EMBEDDER, SETTINGS_PAGE

    assert SETTINGS_PAGE == ROUTE
    assert ROUTE in NO_EMBEDDER
