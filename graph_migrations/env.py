"""Points runic.migrate at the same graph the application talks to.

Executed on every ``runic`` call. Host, port and graph name come from
:class:`~mailarc_core.graph.config.GraphConfig` rather than from a second set
of environment variables, so a migration can never be applied to a different
graph than the one the app reads — ``app_graph_host`` and friends move both.

The scaffold lives in ``graph_migrations/`` and not in runic's default
``runic/``: a package directory of that name at the repository root shadows
the installed ``runic`` package on ``sys.path``. Every command therefore needs
``--config graph_migrations/env.py`` — ``task graph:*`` passes it.

Only FalkorDB is wired up. It is the backend this project ships and the only
one it can start locally; a second one gets its branch here when it exists.
"""

from runic.migrate import context
from runic.migrate.adapters import create_adapter

from mailarc_core.graph.config import GraphConfig

config = GraphConfig()

adapter = create_adapter(
    "falkordb",
    host=config.host,
    port=config.port,
    username=config.username,
    password=config.password.get_secret_value() if config.password else None,
    graph_name=config.graph_name,
)

context.configure(adapter, track_checksums=True, track_installed_by=False)
