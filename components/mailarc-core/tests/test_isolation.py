"""The import rules that keep the components apart, checked from outside.

Several probes, because the rules differ. Everything below the UI has to stay
usable without a browser; :mod:`mailarc_ui` needs Reflex and is deliberately
exempt. The ban on ``runic.rag`` binds all six: the graph is written from
message headers, and nothing a model invents may join that ground truth. And no
component may import ``app`` — the rule that made the MCP server movable in the
first place, and the one that costs the most to discover late.

A subprocess, because the application's own tests import Reflex into the shared
interpreter, which would make an in-process check meaningless. Submodules are
found with :func:`pkgutil.walk_packages`, so the probe keeps covering whatever
later phases add instead of going stale against a hand-written list.
"""

import importlib.util
import subprocess
import sys

import pytest

FORBIDDEN = ("reflex", "appkit_mantine", "appkit_ui", "appkit_user")

OPTIONAL = ("mailarc_mcp",)
"""Components an installation is allowed not to have.

``mailarc-mcp`` is the ``mcp`` extra: ``uv sync`` resolves without it so the
desktop bundle carries no ``fastmcp``, and this file has to survive that. Every
probe below therefore filters its packages through :func:`_present` — a rule
that fails with "no module named mailarc_mcp" on the very installation the
extra exists to produce would be a test punishing the feature.
"""

HEADLESS = (
    "mailarc_core",
    "mailarc_sync",
    "mailarc_analytics",
    "mailarc_google",
    "mailarc_mcp",
)
"""The five that must work with no browser in the room.

``mailarc_mcp`` belongs here and not in an exemption: it imports ``fastmcp``,
which is neither Reflex nor an appkit UI package, and a server that answers a
language model over a pipe has even less business holding a web framework than
a worker does.
"""

EVERY_COMPONENT = (*HEADLESS, "mailarc_ui")

MUST_NOT_SEE_SYNC = ("mailarc_analytics", "mailarc_google", "mailarc_mcp")
"""The components §6's import table forbids ``mailarc_sync`` to.

Both rules were enforced by nobody: ``HEADLESS`` lumps the headless components
into one probe and intersects the result with ``FORBIDDEN``, which holds only UI
frameworks — so importing ``mailarc_sync`` from ``mailarc_analytics`` passed
every test in the repository, while ``mailarc-analytics/README.md`` states the
rule under "Rules" and closes by saying this file enforces it. The rule does
hold today; nothing held it.

Analysis runs *after* an import, not inside one, and a provider is a way of
fetching mail rather than a way of scheduling it. The MCP server is the third
case and the plainest: it answers questions about an archive that already
exists, so it has no reason to know how mail gets into one. Any of them
reaching for the engine would turn a layer into a cycle.
"""

PROBE = """
import importlib, pkgutil, sys

for name in {packages!r}:
    package = importlib.import_module(name)
    for found in pkgutil.walk_packages(package.__path__, name + "."):
        importlib.import_module(found.name)

print(",".join(sorted(sys.modules)))
"""


def _present(packages: tuple[str, ...]) -> tuple[str, ...]:
    """Only the packages this installation actually has.

    ``find_spec`` rather than a ``try: import``, because importing is what the
    probe is for and doing it here would pull the package into *this*
    interpreter — which is the thing a subprocess probe exists to avoid.
    """
    return tuple(name for name in packages if importlib.util.find_spec(name))


def _modules_after_importing(packages: tuple[str, ...]) -> set[str]:
    """What a fresh interpreter holds once *packages* and their submodules are in."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE.format(packages=packages)],
        capture_output=True,
        text=True,
        check=False,
    )
    # A forbidden import can fail rather than land in sys.modules — `runic.rag`
    # needs extras this project never installs. Report that, don't drown in a
    # CalledProcessError.
    assert result.returncode == 0, (
        f"importing {list(packages)} failed outright:\n{result.stderr}"
    )
    return set(result.stdout.strip().split(","))


def test_components_below_the_ui_pull_in_no_ui_framework() -> None:
    probed = _present(HEADLESS)
    offenders = _modules_after_importing(probed) & set(FORBIDDEN)

    assert not offenders, (
        f"{list(probed)} dragged in {sorted(offenders)} — a CLI, a worker or "
        "a test must be able to use them without a browser"
    )


@pytest.mark.parametrize("package", MUST_NOT_SEE_SYNC)
def test_a_component_below_the_engine_does_not_import_it(package: str) -> None:
    """One package per probe, or the answer means nothing.

    The forbidden module is itself one of the components, so a probe that
    imported several at once would find ``mailarc_sync`` in ``sys.modules``
    because it asked for it.
    """
    if package not in _present((package,)):
        pytest.skip(f"{package} is not installed — see OPTIONAL")
    imported = _modules_after_importing((package,))

    assert "mailarc_sync" not in imported, (
        f"{package} imported mailarc_sync — §6's import table has the engine "
        "above both, and a component that reaches back up makes the layering "
        "a cycle"
    )


def test_the_engine_does_not_import_a_provider() -> None:
    """The other direction of the same rule, and the one nothing held either.

    ``MUST_NOT_SEE_SYNC`` covers a provider reaching up; this covers the engine
    reaching down, which §6's table forbids in the words "``mailarc-sync`` may
    not import ``mailarc_google``". Phase 7 is where the temptation appears:
    the interval scheduler has to know whether a mailbox can answer "what
    changed since?", and the shortest way to find out is to import Gmail and
    ask. It asks a ``ProviderDescriptor`` off the registry instead, which is
    what keeps the engine drivable by a provider written after it.
    """
    imported = _modules_after_importing(("mailarc_sync",))

    assert "mailarc_google" not in imported, (
        "mailarc_sync imported mailarc_google — the engine drives a mailbox "
        "through the port and must not be able to name one, or the next "
        "provider costs a change in the engine"
    )


def test_no_component_reaches_for_graphrag() -> None:
    imported = _modules_after_importing(_present(EVERY_COMPONENT))

    assert "runic.rag" not in imported, (
        "a component imported runic.rag — email already carries an exact graph "
        "in its headers, so an LLM extraction would only make it probabilistic"
    )


def test_no_headless_component_imports_the_application() -> None:
    """The rule the whole layering rests on, and the one nothing held either.

    ``app`` is the composition root: it reads configuration, names Gmail, and
    knows which graph this installation uses. A component that reaches back into
    it inherits all three and stops being something a CLI, a worker or another
    deployment can install — which is exactly what the MCP server had become
    before it was pulled out of ``app/mcp_server/`` into ``mailarc-mcp``, and the
    reason the desktop bundle could not leave ``fastmcp`` behind.

    ``app`` is importable from this working directory, so the probe is
    meaningful: it fails by finding the module, not by failing to.

    :data:`EVERY_COMPONENT`, ``mailarc_ui`` included. It was ``HEADLESS`` for as
    long as ``rxconfig.py`` said ``from app import settings``: importing
    anything that touches Reflex can call ``reflex_base.config``, which imports
    ``rxconfig``, so ``app`` landed in ``sys.modules`` two frames below
    ``appkit_mantine`` without ``mailarc_ui`` having named it — and an assertion
    that fires on somebody else's import would be deleted the first time it went
    off. ``rxconfig.py`` now reads its configuration through ``appkit_commons``
    alone and names nothing of ours, so the probe can tell the two apart and the
    rule that always bound the UI is finally checked on it.
    """
    imported = _modules_after_importing(_present(EVERY_COMPONENT))

    assert "app" not in imported, (
        "a component imported app — the composition root is above every "
        "component, and one that reaches up cannot be installed without it"
    )
