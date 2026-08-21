"""The review page: it builds, it is reachable, it primes itself, it is gated."""

import subprocess
import sys
from typing import Any

import reflex as rx
from reflex.page import DECORATED_PAGES

from app.components.navbar import app_navbar
from app.pages.mail_review import ROUTE, mail_review_page

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""

BOOT_PROBE = f"""
import app.app
from appkit_commons.registry import service_registry
from mailarc_core import ArchiveReader
from reflex.page import DECORATED_PAGES

routes = {{kwargs.get("route") for pages in DECORATED_PAGES.values() for _, kwargs in pages}}
print({ROUTE!r} in routes, service_registry().has(ArchiveReader), sep=",")
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
    assert isinstance(mail_review_page(), rx.Component)


def test_the_navbar_links_to_the_page() -> None:
    assert ROUTE in _link_targets(app_navbar().render())


def test_the_page_answers_at_that_route() -> None:
    assert _page_kwargs(ROUTE)["title"] == "Review"


def test_the_list_is_primed_on_load() -> None:
    assert "MessageReviewState.load" in _handler_names(_page_kwargs(ROUTE))


def test_the_page_is_admin_only() -> None:
    """The archive is every mailbox of the installation — everybody's mail."""
    assert _gate_of(mail_review_page) is True


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
