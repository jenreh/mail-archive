"""The import rules that keep the components apart, checked from outside.

Two probes, because the rules differ. Everything below the UI has to stay
usable without a browser; :mod:`mailarc_ui` needs Reflex and is deliberately
exempt. The ban on ``runic.rag`` binds all five: the graph is written from
message headers, and nothing a model invents may join that ground truth.

A subprocess, because the application's own tests import Reflex into the shared
interpreter, which would make an in-process check meaningless. Submodules are
found with :func:`pkgutil.walk_packages`, so the probe keeps covering whatever
later phases add instead of going stale against a hand-written list.
"""

import subprocess
import sys

FORBIDDEN = ("reflex", "appkit_mantine", "appkit_ui", "appkit_user")

HEADLESS = ("mailarc_core", "mailarc_sync", "mailarc_analytics", "mailarc_google")
EVERY_COMPONENT = (*HEADLESS, "mailarc_ui")

PROBE = """
import importlib, pkgutil, sys

for name in {packages!r}:
    package = importlib.import_module(name)
    for found in pkgutil.walk_packages(package.__path__, name + "."):
        importlib.import_module(found.name)

print(",".join(sorted(sys.modules)))
"""


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
    offenders = _modules_after_importing(HEADLESS) & set(FORBIDDEN)

    assert not offenders, (
        f"{list(HEADLESS)} dragged in {sorted(offenders)} — a CLI, a worker or "
        "a test must be able to use them without a browser"
    )


def test_no_component_reaches_for_graphrag() -> None:
    imported = _modules_after_importing(EVERY_COMPONENT)

    assert "runic.rag" not in imported, (
        "a component imported runic.rag — email already carries an exact graph "
        "in its headers, so an LLM extraction would only make it probabilistic"
    )
