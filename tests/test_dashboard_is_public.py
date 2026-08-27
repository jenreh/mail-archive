"""``/`` answers without a sign-in. This is the file that pins A4.

The requirement — "user pages need no login" (§1.1, A4) — has exactly one
mechanical expression: the ``on_load`` chain Reflex was handed for ``/`` does
not contain ``LoginState.check_auth``. Everything else about the dashboard
being public is a consequence of that. appkit's three other page decorators put
that handler first and it redirects to ``/login``, so a page built with any of
them is a page a visitor never sees, however public the rest of it looks.

Its own file rather than a case in ``components/mailarc-ui/tests/test_ui_pages.py``
because it is not a claim about a page — it is the claim that separates this
application from the one before it, and it should fail on its own line with its
own name when somebody swaps one decorator for another.

The complement is asserted next door: an administrator's page *does* carry the
handler, so this check cannot pass by looking at a registry that is empty or by
reading an ``on_load`` shape that no longer exists.
"""

from typing import Any

from reflex.page import DECORATED_PAGES

from mailarc_ui.pages import dashboard, status
from mailarc_ui.shell import routes

CHECK_AUTH = "LoginState.check_auth"
"""The handler appkit puts in front of every authenticated page's ``on_load``.

By qualified name, because that is what survives the trip through Reflex's
registry: what is stored is an ``EventHandler`` or an ``EventSpec``, and the
function behind either is the only stable thing to compare.
"""


def _page_kwargs(route: str) -> dict[str, Any]:
    """What Reflex was told about the page at this route."""
    for pages in DECORATED_PAGES.values():
        for _, kwargs in pages:
            if kwargs.get("route") == route:
                return kwargs
    raise AssertionError(f"no page is registered under {route!r}")


def _handler_names(route: str) -> set[str]:
    """The handlers a route's ``on_load`` chain fires, by qualified name.

    Two shapes, because ``on_load`` holds both: an ``EventHandler`` carries its
    function directly, and an ``EventSpec`` — what ``LoadingState.set_is_loading(False)``
    evaluates to — carries a handler that carries it.
    """
    names: set[str] = set()
    for handler in _page_kwargs(route).get("on_load") or []:
        if (fn := getattr(handler, "fn", None)) is not None:
            names.add(fn.__qualname__)
        elif (spec := getattr(handler, "handler", None)) is not None:
            names.add(spec.fn.__qualname__)
    return names


def test_the_dashboard_is_reachable_without_signing_in() -> None:
    """A4, as one assertion: no ``check_auth`` in front of ``/``."""
    assert CHECK_AUTH not in _handler_names(dashboard.ROUTE)


def test_the_dashboard_still_primes_itself() -> None:
    """Public is not the same as inert.

    A decorator that dropped the whole chain would pass the test above and
    leave the page rendering six empty cards for ever.
    """
    assert "DashboardState.load" in _handler_names(dashboard.ROUTE)


def test_an_administrator_page_does_carry_the_check() -> None:
    """The negative control, without which the first test proves nothing.

    If appkit renamed the handler, or Reflex changed how an ``on_load`` chain
    is stored, ``_handler_names`` would answer with a set that contains no
    ``check_auth`` for any page at all — and the assertion above would go green
    for the wrong reason.
    """
    assert CHECK_AUTH in _handler_names(status.ROUTE)


def test_the_dashboard_answers_at_the_index() -> None:
    """The route table and the page have to agree about which page this is."""
    assert dashboard.ROUTE == routes.DASHBOARD
    assert _page_kwargs(routes.DASHBOARD)["title"] == "Dashboard"
