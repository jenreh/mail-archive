"""``task graph:*`` has to obey ``PROFILES``, and once did not.

``graph_migrations/env.py`` used to build a bare ``GraphConfig()``, which reads
nothing out of the profile files at all: ``YamlConfigSettingsSource`` filters
the YAML by the fields of the class asking for it, and the top-level keys are
``profile`` / ``reflex`` / ``app`` — none of them a ``GraphConfig`` field. So
every ``runic`` command resolved to ``127.0.0.1:6379 / mail-archive`` under
every profile: the developer's live server, holding the real archive. A new
graph migration shipped in the same change, which makes "apply the graph
migration" a natural next instruction and that resolution a live hazard.

Asserted in a subprocess under a profile that moves the answer, because that is
the only shape that can tell the two implementations apart. Inside this suite
both a bare ``GraphConfig()`` and the composed one land on the sandbox — the
root ``conftest.py`` sees to that — so an in-process assertion would pass
either way and prove nothing.

Nothing connects: ``create_adapter`` is replaced before the module is imported,
so the probe reports the host, port and graph name that *would* have been
migrated and touches no server.
"""

import json
import os
import subprocess
import sys

PROBE = """
import json, sys, types

# Stand in for runic's adapter factory before `graph_migrations.env` imports it,
# so the probe reports what a migration would have been pointed at without
# opening a connection to it.
seen = {}

import runic.migrate.adapters as adapters
import runic.migrate as migrate

adapters.create_adapter = lambda backend, **kwargs: seen.update(
    backend=backend, **kwargs
) or object()
migrate.context.configure = lambda *args, **kwargs: None

sys.argv = ["probe"]
exec(open("graph_migrations/env.py").read(), {"__name__": "graph_env"})

print(json.dumps({k: str(v) for k, v in seen.items() if k != "password"}))
"""


def _adapter_under(profile: str) -> dict[str, str]:
    """What ``graph_migrations/env.py`` would migrate, under *profile*."""
    environment = {**os.environ, "PROFILES": profile}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, f"the probe failed:\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_graph_commands_follow_the_active_profile() -> None:
    """``agent_test`` names a different port, a different name, its own data.

    The three values that keep an agent off the real archive. A bare
    ``GraphConfig()`` answered ``6379 / mail-archive`` here — the live one.
    """
    adapter = _adapter_under("agent_test")

    assert adapter["port"] == "6399"
    assert adapter["graph_name"] == "mail-archive-agent"


def test_the_default_profile_still_names_the_application_graph() -> None:
    """The other direction, so the fix is a redirection and not a break.

    ``local`` is what ``taskfiles/Taskfile.graph.yml`` defaults to, and it has
    to keep pointing at the graph the application actually reads — a migration
    that quietly stopped reaching it would be the opposite failure.
    """
    adapter = _adapter_under("local")

    assert adapter["port"] == "6379"
    assert adapter["graph_name"] == "mail-archive"
