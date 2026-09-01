"""Reflex's own configuration — ports, database URLs and plugins.

This module deliberately imports nothing from ``app/`` or ``components/``.

Reflex 0.9.9 changed how it loads this file (``reflex_base.config._get_config``):
before every load it drops ``rxconfig`` *and every module that module imported
whose file sits inside the project root but outside ``site-packages``* from
``sys.modules``, so each load re-reads the configuration from disk. Our
components are uv workspace members installed editable from ``components/*/src``
— inside the project root — so ``from app import settings`` put the whole stack
(100 modules) into that eviction set and re-executed it on the second load.

``mailarc_core.database.entities`` cannot survive that. It registers its tables
on appkit_commons' SQLAlchemy ``Base``, which lives in site-packages and is
therefore *not* evicted, so the re-execution walked into::

    sqlalchemy.exc.InvalidRequestError:
        Table 'mail_accounts' is already defined for this MetaData instance.

Reading the configuration through appkit_commons alone keeps that eviction set
empty, so the application stack is imported exactly once per process — by
``app/__init__.py``, when Reflex imports the app module.
"""

import logging

import reflex as rx
from appkit_commons.configuration.configuration import (
    ApplicationConfig,
    Configuration,
    ReflexConfig,
)
from appkit_commons.configuration.logging import init_logging
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.registry import service_registry

ASYNC_DRIVER = "+aiosqlite"
"""Duplicated from ``mailarc_core.database.sqlite``, which owns this rule.

Reflex opens a blocking session as well as an async one, and the configured URL
only carries the async driver. Importing the canonical ``sync_database_url()``
would import ``mailarc_core.database`` — and with it the entities this module's
docstring is about.
"""

_registry = service_registry()

#: Ports and database URL only — everything Reflex itself needs. When Reflex
#: imports the app module, ``app/__init__.py`` configures the full ``AppConfig``
#: and registers it; a config load after that point reuses it rather than
#: replacing it with this narrower view. Nothing is registered from here, so
#: the order the two run in cannot matter.
settings: Configuration[ApplicationConfig] = (
    _registry.get(Configuration)
    if _registry.has(Configuration)
    else Configuration[ApplicationConfig](_env_file="/.env")
)

init_logging(settings)
logger = logging.getLogger(__name__)

database: DatabaseConfig | None = settings.app.database
reflex: ReflexConfig | None = settings.reflex

config = rx.Config(
    app_name="app",
    frontend_port=reflex.frontend_port if reflex else 8080,
    backend_port=reflex.backend_port if reflex else 3030,
    # Reflex opens both a blocking and an async session; the configured URL
    # only carries the async driver.
    db_url=database.url.replace(ASYNC_DRIVER, "", 1) if database else None,
    async_db_url=database.url if database else None,
    telemetry_enabled=False,
    show_built_with_reflex=False,
    plugins=[
        rx.plugins.TailwindV4Plugin(),
        rx.plugins.RadixThemesPlugin(),
    ],
    disable_plugins=[rx.plugins.SitemapPlugin],
)
