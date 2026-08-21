import logging
import os
from functools import lru_cache

from appkit_commons.configuration.configuration import (
    ApplicationConfig,
    Configuration,
)
from appkit_commons.registry import service_registry
from appkit_user.configuration import AuthenticationConfiguration
from pydantic import Field

from mailarc_core import ArchiveConfig, GraphConfig
from mailarc_core.database import sqlite
from mailarc_core.mail.config import MailConfig
from mailarc_google.source.config import GmailConfig
from mailarc_sync.engine.config import SyncConfig

logger = logging.getLogger(__name__)


class AppConfig(ApplicationConfig):
    authentication: AuthenticationConfiguration
    graph: GraphConfig = Field(default_factory=GraphConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    google: GmailConfig = Field(default_factory=GmailConfig)


@lru_cache(maxsize=1)
def configure() -> Configuration[AppConfig]:
    logger.debug("--- Configuring application settings ---")
    logger.debug("Active profiles: %s", os.environ.get("PROFILES", "<none>"))
    configuration = service_registry().configure(
        AppConfig,
        env_file="/.env",
    )
    sqlite.prepare(configuration.app.database)
    return configuration
