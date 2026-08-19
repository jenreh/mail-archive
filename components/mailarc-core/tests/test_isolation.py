"""The contract that justifies this component: no UI framework, ever.

Checked in a subprocess because the application's own tests import Reflex into
the shared interpreter, which would make an in-process check meaningless.
"""

import subprocess
import sys

FORBIDDEN = ("reflex", "appkit_mantine", "appkit_ui", "appkit_user")

PROBE = """
import importlib, sys

for name in (
    "mailarc_core",
    "mailarc_core.database",
    "mailarc_core.database.sqlite",
    "mailarc_core.graph",
    "mailarc_core.graph.admin",
    "mailarc_core.graph.client",
    "mailarc_core.graph.config",
    "mailarc_core.graph.model",
    "mailarc_core.graph.runtime",
    "mailarc_core.graph.server",
    "mailarc_core.graph.status",
):
    importlib.import_module(name)

print(",".join(sorted(m for m in sys.modules if "." not in m)))
"""


def test_importing_the_core_pulls_in_no_ui_framework() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    imported = set(result.stdout.strip().split(","))

    assert not imported & set(FORBIDDEN), (
        f"mailarc_core dragged in {sorted(imported & set(FORBIDDEN))} — "
        "the core must be usable without a browser"
    )
