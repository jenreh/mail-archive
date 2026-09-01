import logging
import os
from functools import lru_cache

from appkit_commons.configuration.configuration import (
    ApplicationConfig,
    Configuration,
)
from appkit_commons.registry import service_registry
from pydantic import Field

from mailarc_analytics import AnalyticsConfig
from mailarc_analytics.semantic import SemanticConfig
from mailarc_core import ArchiveConfig, GraphConfig
from mailarc_core.database import sqlite
from mailarc_core.mail.config import MailConfig
from mailarc_google.source.config import GmailConfig
from mailarc_imap.source.config import ImapConfig
from mailarc_m365.source.config import M365Config
from mailarc_sync.engine.config import SyncConfig

logger = logging.getLogger(__name__)


class AppConfig(ApplicationConfig):
    graph: GraphConfig = Field(default_factory=GraphConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)
    semantic: SemanticConfig = Field(default_factory=SemanticConfig)
    """The embedder, if this installation has one. Off by default (§7.4).

    Carried here rather than left to ``app_semantic_*`` alone because
    ``model_config['extra']`` is ``ignore``: an ``app.semantic.provider: ollama``
    block in ``configuration/config.yaml`` — the documented way every other
    component on this list is configured — reached nothing at all while this
    field was missing, and was dropped without a word.
    """

    mail: MailConfig = Field(default_factory=MailConfig)
    google: GmailConfig = Field(default_factory=GmailConfig)
    imap: ImapConfig = Field(default_factory=ImapConfig)
    m365: M365Config = Field(default_factory=M365Config)
    """One field per provider component, for the reason ``semantic`` records above.

    Neither of these is optional decoration. ``model_config['extra']`` is
    ``ignore``, so an ``app.imap`` or ``app.m365`` block in
    ``configuration/config.yaml`` — the documented way every other component on
    this list is configured — would be dropped without a word if the field were
    missing, and ``app/composition.py`` would hand each provider a config built
    from the environment alone. For ``m365`` that is the difference between an
    installation that has an Entra application and one that silently does not —
    its ``client_id`` and ``client_secret`` are ``secret:`` references, and a
    reference nothing reads is a registration nobody signs in with.

    ``config.yaml`` ships both blocks commented out; the field is what makes
    uncommenting them mean something.
    """


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
