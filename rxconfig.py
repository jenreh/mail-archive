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

_registry = service_registry()

settings: Configuration[ApplicationConfig] = (
    _registry.get(Configuration)
    if _registry.has(Configuration)
    else Configuration[ApplicationConfig](_env_file="/.env")
)

init_logging(settings)
logger = logging.getLogger(__name__)

database: DatabaseConfig | None = settings.app.database
reflex: ReflexConfig | None = settings.reflex

FRONTEND_PORT = reflex.frontend_port if reflex else 8080
BACKEND_PORT = reflex.backend_port if reflex else 3030
BACKEND_HOST = "127.0.0.1"
ASYNC_DRIVER = "+aiosqlite"


CORS_ALLOWED_ORIGINS = [
    f"http://{host}:{port}"
    for port in sorted({FRONTEND_PORT, BACKEND_PORT})
    for host in ("127.0.0.1", "localhost")
] + ([reflex.deploy_url] if reflex and reflex.deploy_url else [])

config = rx.Config(
    app_name="app",
    frontend_port=FRONTEND_PORT,
    backend_port=BACKEND_PORT,
    backend_host=BACKEND_HOST,
    cors_allowed_origins=CORS_ALLOWED_ORIGINS,
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
