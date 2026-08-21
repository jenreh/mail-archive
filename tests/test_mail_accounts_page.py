"""The mail accounts page: it builds, it is reachable, and it primes itself."""

import subprocess
import sys
from typing import Any

import reflex as rx
from reflex.page import DECORATED_PAGES

from app.components.navbar import app_navbar
from app.pages.home import home_page
from app.pages.mail_accounts import ROUTE, mail_accounts_page
from app.pages.users import users_page

LINK_TARGET = 'to:"'
"""How a rendered ``rx.link`` carries its destination: a router link's prop."""

BOOT_PROBE = f"""
import app.app
from appkit_commons.registry import service_registry
from mailarc_sync.engine import ProviderRegistry
from reflex.page import DECORATED_PAGES

routes = {{kwargs.get("route") for pages in DECORATED_PAGES.values() for _, kwargs in pages}}
print({ROUTE!r} in routes, service_registry().has(ProviderRegistry), sep=",")
"""
"""What starting the application has to leave behind, asked from outside."""


def _link_targets(node: dict[str, Any]) -> list[str]:
    """Every route a rendered component points at.

    Read off the render rather than off the source, because the render is what
    the browser is handed — a constant nobody links to would prove nothing.
    """
    targets = [
        prop.removeprefix(LINK_TARGET).removesuffix('"')
        for prop in node.get("props", [])
        if prop.startswith(LINK_TARGET)
    ]
    for child in node.get("children", []):
        targets.extend(_link_targets(child))
    return targets


def _page_kwargs(route: str) -> dict[str, Any]:
    """What reflex was told about the page at this route."""
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


def test_the_page_builds() -> None:
    """A prop appkit_mantine does not have only shows up when it is built."""
    assert isinstance(mail_accounts_page(), rx.Component)


def test_the_navbar_links_to_the_page() -> None:
    """A typo in either half is a 404 nobody notices until a human clicks it."""
    assert ROUTE in _link_targets(app_navbar().render())


def test_the_page_answers_at_that_route() -> None:
    """Importing the module is what registers it; app.py does the importing."""
    assert _page_kwargs(ROUTE)["title"] == "Mail accounts"


def test_starting_the_application_wires_the_page_up() -> None:
    """``app/app.py`` is the only place that imports this page and the only
    place that publishes the provider registry, and neither line shows up in a
    test that imports the page itself — a missing one is a 404 and an accounts
    page with no providers, at runtime and nowhere earlier.

    In its own interpreter, for the reason ``tests/test_worker.py`` gives:
    importing the whole application here would register every page of it for
    every other test in this process.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", BOOT_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"importing app.app failed:\n{result.stderr}"
    # The last line, because a booting application logs to stdout as well.
    answer = result.stdout.strip().splitlines()[-1]

    assert answer == "True,True", (
        f"the application started without wiring the page up:\n{result.stdout}"
    )


def test_both_halves_are_primed_on_load() -> None:
    """Without the accounts load there are no providers to pick and no
    mailboxes to import; without the refresh the panel opens blank."""
    names = _handler_names(_page_kwargs(ROUTE))

    assert "MailAccountState.load" in names
    assert "ImportJobState.refresh" in names


def _gate_of(page: Any) -> bool:
    """Whether `@authenticated` was told to admin-gate this page.

    Read off the decorator's closure because there is nowhere else to read it:
    `admin_only` is a plain bool, so `rx.cond` folds it away at build time and
    the rendered tree of a gated page is identical to an ungated one. A
    security control with no runtime trace still deserves a test.
    """
    closure = dict(zip(page.__code__.co_freevars, page.__closure__ or (), strict=True))
    cell = closure.get("admin_only")
    if cell is None:
        raise AssertionError("this page was not built by @authenticated")
    return bool(cell.cell_contents)


def test_the_page_is_admin_only() -> None:
    """`mail_accounts` has no owner column, so this page shows *every* mailbox.

    Left at plain `@authenticated` — whose `admin_only` defaults to False — any
    account holder on a multi-user deployment could list, connect, import and
    delete other people's mailboxes, and the archive behind them is everybody's
    private mail. `/admin/users` is gated for the same reason.
    """
    assert _gate_of(mail_accounts_page) is True


def test_the_gate_is_read_from_somewhere_that_can_be_false() -> None:
    """Otherwise the assertion above would pass on any page at all."""
    assert _gate_of(users_page) is True
    assert _gate_of(home_page) is False
