"""What starting the application actually leaves behind.

The one test in the root suite that still has to exist after the interface
moved into ``mailarc-ui``. Everything a page claims about itself — it builds,
it answers at its route, it primes itself, it is gated — is now asserted in
``components/mailarc-ui/tests/test_ui_pages.py`` against the page module. What
cannot be asserted there is the wiring: ``app/app.py`` is the only place that
imports those modules and the only place that publishes the services they read
from, and neither line shows up in a test that imports a page directly. A
missing import is a 404; a missing ``publish_*`` is a page that loads and then
says a developer error to a user.

**In its own interpreter**, for the reason ``tests/test_worker.py`` gives and
one more of its own: importing ``app.app`` registers every page of the
application into Reflex's module-level ``DECORATED_PAGES``, and that registry
outlives the test — every later test in the process would then be looking at an
application it did not start. The subprocess inherits this run's sealed
environment, so the boot it performs reads the sandbox and never ``.state``.
"""

import json
import subprocess
import sys
from typing import Any

import pytest

from mailarc_ui.shell import routes

SERVICES: tuple[tuple[str, str, str], ...] = (
    ("provider_registry", "mailarc_sync.engine", "ProviderRegistry"),
    ("archive_reader", "mailarc_core", "ArchiveReader"),
    ("analytics_reader", "mailarc_analytics", "AnalyticsReader"),
    ("semantic_search", "mailarc_analytics.semantic", "SemanticSearch"),
    ("semantic_control", "mailarc_analytics.semantic", "SemanticControl"),
    ("graph_health", "mailarc_core.graph", "GraphHealth"),
    ("storage_reader", "mailarc_core.storage", "StorageReader"),
)
"""Every service a page reads out of the registry, and where its type lives.

``mailarc-ui`` may not import ``app``, so each of these reaches the browser
half through the service registry and through nothing else. One that is not
published is a panel whose only output is the sentence its lookup raises.
"""

EXPECTED_ROUTES: frozenset[str] = frozenset(
    {
        routes.SEARCH,
        routes.DASHBOARD,
        routes.INSIGHTS,
        routes.REVIEW,
        routes.ACCOUNTS,
        routes.EMBEDDER,
        routes.GRAPH_STATUS,
    }
)
"""What a booted application has to answer at.

``/`` is on the list and is the one that needs saying: it is the address a
visitor arrives at and the page the whole application is built around, and it
reaches Reflex through a decorator of this project's own. A boot that quietly
dropped its import would leave the front door of the archive at a 404 while
every test about the search itself went on passing.

There are no appkit routes on the list any more. The archive is a desktop
application with no sign-in, so ``/login`` and the two password-reset pages
are not merely unregistered — they are not part of this interface, and a boot
that produced one would mean the dependency came back.

A subset check rather than an equality one, so that a page added next does not
fail here before it fails anywhere that could explain why.
"""

BOOT_PROBE = f"""
import importlib
import json

import app.app
from appkit_commons.registry import service_registry
from reflex.page import DECORATED_PAGES

services = {{
    name: service_registry().has(getattr(importlib.import_module(module), attribute))
    for name, module, attribute in {SERVICES!r}
}}
routes = sorted(
    kwargs["route"]
    for pages in DECORATED_PAGES.values()
    for _, kwargs in pages
    if kwargs.get("route")
)
print("BOOT " + json.dumps({{"routes": routes, "services": services}}))
"""
"""What starting the application has to leave behind, asked from outside."""


@pytest.fixture(scope="module")
def booted() -> dict[str, Any]:
    """Start the whole application once and report what it wired up.

    Module-scoped because a boot imports Reflex, appkit and every component,
    and doing that per assertion would make this file the slowest in the suite
    for no additional coverage.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", BOOT_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"importing app.app failed:\n{result.stderr}"
    # The marker line, because a booting application logs to stdout as well.
    reported = [line for line in result.stdout.splitlines() if line.startswith("BOOT ")]
    assert reported, f"the probe printed nothing:\n{result.stdout}\n{result.stderr}"
    return json.loads(reported[-1].removeprefix("BOOT "))


def test_the_application_starts(booted: dict[str, Any]) -> None:
    """The premise of everything below, stated on its own so a failure here
    reads as "it did not start" rather than as a missing route."""
    assert booted["routes"], "a booted application serves no page at all"


def test_every_page_is_registered(booted: dict[str, Any]) -> None:
    """``app/app.py`` imports the page modules for their registration side
    effect alone, so a dropped import is a 404 and nothing earlier."""
    missing = EXPECTED_ROUTES - set(booted["routes"])

    assert not missing, f"the application starts without {sorted(missing)}"


def test_the_administration_is_reachable(booted: dict[str, Any]) -> None:
    """Every ``/admin/*`` route the sidebar offers has to be one of them.

    The other direction of the check above: the navigation is built from
    ``routes.py`` and so is this, so a route added to the table and to the
    sidebar but never imported would be a link straight into a 404.
    """
    admin = {route for route in routes.ALL_ROUTES if route.startswith("/admin/")}

    assert admin, "the route table declares no administration at all"
    assert admin <= set(booted["routes"])


def test_no_route_is_served_twice(booted: dict[str, Any]) -> None:
    """Two page modules answering at one path is what a copied ``ROUTE``
    produces, and Reflex picks one of them without saying which."""
    duplicates = sorted(
        {route for route in booted["routes"] if booted["routes"].count(route) > 1}
    )

    assert not duplicates, f"{duplicates} are each registered more than once"


def test_every_service_a_page_reads_is_published(booted: dict[str, Any]) -> None:
    """The half of the wiring that has no visible symptom until a user clicks.

    A component may not import ``app``, so each of these travels from the
    composition root to the browser through the service registry. One that is
    not published leaves the page it feeds rendering its lookup's error text.
    """
    unpublished = sorted(
        name for name, published in booted["services"].items() if not published
    )

    assert not unpublished, f"the application starts without publishing {unpublished}"
