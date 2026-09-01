"""Where the archive puts the originals, and how much body the graph keeps.

Two settings, both of them limits rather than switches: everything else about
an import — which account, which folder, how often — is state in SQLite, not
configuration.
"""

from pathlib import Path

from appkit_commons.configuration.base import BaseConfig
from pydantic_settings import SettingsConfigDict


class ArchiveConfig(BaseConfig):
    """The blob store's root and the cap on the text that goes into the graph."""

    model_config = SettingsConfigDict(
        env_prefix="app_archive_",
        env_file=".env",
        populate_by_name=True,
    )

    store_dir: Path = Path(".state/mailstore")
    """Root of the content-addressed store holding the original ``.eml`` bytes.

    Under ``.state`` with the graph's own data directory, so one path is all a
    backup has to know about.
    """

    body_text_limit: int = 65536
    """Characters of ``body_text`` the ``Message`` node keeps — 64 KB.

    The graph holds text so full-text search has something to match; it is not
    the archive. The whole message stays on disk in the blob store, so a longer
    body is never lost, only unindexed past this point.
    """
