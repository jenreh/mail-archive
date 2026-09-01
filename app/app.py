"""The application: what it publishes, which pages it serves, what it owns.

Everything visual lives in ``mailarc-ui`` now — the styles, the theme, the
shell, the pages. What is left here is the five things only the application
layer can do: set the log levels, hand the composition root's objects to the
browser half through the service registry, register the archive's Mantine
theme before ``rx.App`` builds the provider that reads it, name the page
modules whose import registers them, and own the three background lifespans.
"""

import logging

import reflex as rx
from appkit_commons.middleware import ForceHTTPSMiddleware
from starlette.types import ASGIApp

from app.composition import (
    graph_server_lifespan,
    publish_account_eraser,
    publish_analytics_reader,
    publish_archive_reader,
    publish_graph_health,
    publish_provider_registry,
    publish_semantic_control,
    publish_semantic_search,
    publish_storage_reader,
    semantic_settings_lifespan,
    sync_worker_lifespan,
)
from mailarc_ui.pages import (  # noqa: F401  # imported for their route registration
    accounts,
    dashboard,
    embedder,
    insights,
    search,
    status,
)
from mailarc_ui.styles import base_style, base_stylesheets
from mailarc_ui.theme import set_mailarc_theme

logging.basicConfig(level=logging.DEBUG)
# Three libraries that write somebody's secret into the log at DEBUG, pinned by
# name so that no root level — this one, or a debug session's, or a future
# edit's — can reach them. An explicit level on a logger beats whatever the
# root says, which is what makes this a floor rather than a preference.
#
# oauthlib and requests-oauthlib log the complete token response, refresh token
# included. aiosqlite logs every statement it executes *with its bound values*
# — measured, not assumed: at root DEBUG an insert into a table with an
# `EncryptedString` column prints as `executing functools.partial(<cursor>,
# 'INSERT INTO ...', ('gAAAAAB...',))`, so `mail_credentials.secret` and
# `semantic_settings.api_key` reach the log as ciphertext and every other bound
# value reaches it in the clear.
#
# Note what the `basicConfig` above does NOT do: appkit's `init_logging` runs
# `dictConfig` later in this import and puts the root back to INFO, so the DEBUG
# asked for here survives only until then. `tests/test_app_logging.py` pins
# that, because the line reads like it decides the level and does not.
for _noisy in ("oauthlib", "requests_oauthlib", "aiosqlite"):
    logging.getLogger(_noisy).setLevel(logging.INFO)
# WARNING and not INFO, which is the whole point: SQLAlchemy gates its
# statement-and-parameter echo on `isEnabledFor(INFO)` against this logger
# rather than on `echo=True`, so INFO here would *switch the echo on*. It is
# off today anyway — sqlalchemy pins its own logger to WARN at import when
# nobody has set one — but that default is somebody else's and is only in force
# while this file is imported after sqlalchemy.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


# Middleware transformer for HTTPS redirect
def add_https_middleware(asgi_app: ASGIApp) -> ASGIApp:
    """Wrap the ASGI app with HTTPS redirect middleware."""
    return ForceHTTPSMiddleware(asgi_app)


# The accounts page reads which providers exist and how a mailbox is cleared
# out again, the search page reads the archive, the insights page reads both
# what was derived from it and the search over it, the status page reads the
# graph server and the dashboard's disk panel reads the paths the archive
# occupies — all of them out of the service registry: `mailarc-ui` is a
# component and may not import `app`, so the composition root leaves its
# decisions there for it. The search is published even with no embedder
# configured: its full-text half needs none, and that is the half a default
# installation depends on. The embedder page gets the two verbs that go the
# other way — read what is in force, and adopt what was just saved — because a
# form that writes a setting nothing re-reads would be a form that silently
# does nothing until the next restart.
publish_provider_registry()
publish_account_eraser()
publish_archive_reader()
publish_analytics_reader()
publish_semantic_search()
publish_semantic_control()
publish_graph_health()
publish_storage_reader()

# The archive's own Mantine theme — the coral accent, the warm grays, Inter and
# the radius scale — before `rx.App`, and that order is the whole requirement:
# the theme is forwarded to the root `MantineProvider` that wraps every page,
# and a provider already built reads whatever was registered when it was.
set_mailarc_theme()

app = rx.App(
    stylesheets=base_stylesheets,
    style=base_style,  # ty: ignore[invalid-argument-type]  # reflex dynamic styling
    api_transformer=[add_https_middleware],
)

# Lay whatever a human stored over the embedder the configuration file
# describes, before the first request. It has to be a lifespan and not a call
# above: the search is published while this module is being imported, which is
# before there is an event loop to read the settings row with. Registered first
# so nothing else starts against the configuration it replaces.
app.register_lifespan_task(semantic_settings_lifespan)

# Own the FalkorDB process for exactly as long as the app runs. In local mode
# this starts the vendored server on boot and stops it on shutdown; in remote
# mode both ends are no-ops.
app.register_lifespan_task(graph_server_lifespan)

# Run the import worker as a child process for as long as the app runs. Off
# under Docker and systemd via `sync.supervise_worker`, where the worker is its
# own unit and a second copy would claim the same jobs.
app.register_lifespan_task(sync_worker_lifespan)
