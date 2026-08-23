"""The insights page: it builds, it is reachable, it primes itself, it is gated.

The same five questions ``test_mail_review_page.py`` asks, because a page is
wired up in exactly five places and four of them are silent when they are
wrong: a route nobody links to, an ``on_load`` nobody fires, a gate nobody set,
and a reader the composition root never published.
"""

import subprocess
import sys
from typing import Any

import reflex as rx
from reflex.page import DECORATED_PAGES

from app.components.navbar import app_navbar
from app.pages.mail_insights import ROUTE, mail_insights_page

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""

BOOT_PROBE = f"""
import app.app
from appkit_commons.registry import service_registry
from mailarc_analytics import AnalyticsReader
from reflex.page import DECORATED_PAGES

routes = {{kwargs.get("route") for pages in DECORATED_PAGES.values() for _, kwargs in pages}}
print({ROUTE!r} in routes, service_registry().has(AnalyticsReader), sep=",")
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
    assert isinstance(mail_insights_page(), rx.Component)  # ty: ignore[call-non-callable]


def test_the_navbar_links_to_the_page() -> None:
    assert ROUTE in _link_targets(app_navbar().render())


def test_the_page_answers_at_that_route() -> None:
    assert _page_kwargs(ROUTE)["title"] == "Insights"


def test_the_panels_are_primed_on_load() -> None:
    """Every panel spins until the first read lands, so a page that never asks
    would show five spinners and no error."""
    assert "AnalyticsInsightsState.load" in _handler_names(_page_kwargs(ROUTE))


def test_the_page_is_admin_only() -> None:
    """A co-recipient listing says who writes to whom, across every mailbox.

    Asserted here, but **not** relied on: ``admin_only`` expands to a
    render-time ``rx.cond``, appkit builds the ``on_load`` chain separately,
    and Reflex runs all of it whatever ``check_auth`` returns. So ``load``
    executes for a logged-out visitor and a signed-in non-admin alike — the DOM
    they get is the no-permission page, the state delta they would get is every
    co-recipient pair in the installation. What actually stops that is
    ``AnalyticsInsightsState._may_read``, server-side, where the data leaves;
    ``TestWhoIsAsking`` in ``test_ui_insights_state.py`` is where that is
    proved. This line is the cosmetic half.
    """
    assert _gate_of(mail_insights_page) is True


def test_starting_the_application_wires_the_page_up() -> None:
    """``app/app.py`` imports the page and publishes the reader, and neither
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
