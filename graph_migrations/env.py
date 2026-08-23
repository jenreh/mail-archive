"""Points runic.migrate at the same graph the application talks to.

Executed on every ``runic`` call. Host, port and graph name come from the
**composed** application configuration rather than from a second set of
environment variables, so a migration can never be applied to a different graph
than the one the app reads.

``from app import settings``, the way ``alembic/env.py`` does it, and not a
bare ``GraphConfig()``. That was the bug this docstring used to describe as a
feature: ``YamlConfigSettingsSource`` filters the YAML by the fields of the
class asking for it, and the top-level keys are ``profile`` / ``reflex`` /
``app`` — none of them a ``GraphConfig`` field — so a bare ``GraphConfig()``
reads *nothing* out of the profile files. Measured before the fix:

    $ PROFILES=agent_test uv run python -c "...GraphConfig()..."
    127.0.0.1 6379 mail-archive .state/falkordb

That is the real archive, under every profile, including the ones that exist
precisely so an agent or a test cannot reach it. ``PROFILES=agent_test task
graph:upgrade`` applied migrations to the developer's live graph. Going through
``AppConfig`` puts ``app.graph`` where the YAML source can see it, which is
what makes ``task graph:*`` profile-aware for real — and what the environment
block in ``taskfiles/Taskfile.graph.yml`` has always claimed.

The scaffold lives in ``graph_migrations/`` and not in runic's default
``runic/``: a package directory of that name at the repository root shadows
the installed ``runic`` package on ``sys.path``. Every command therefore needs
``--config graph_migrations/env.py`` — ``task graph:*`` passes it.

Only FalkorDB is wired up. It is the backend this project ships and the only
one it can start locally; a second one gets its branch here when it exists.
"""

from runic.migrate import context
from runic.migrate.adapters import create_adapter

from app import settings

config = settings.app.graph

adapter = create_adapter(
    "falkordb",
    host=config.host,
    port=config.port,
    username=config.username,
    password=config.password.get_secret_value() if config.password else None,
    graph_name=config.graph_name,
)

context.configure(adapter, track_checksums=True, track_installed_by=False)
