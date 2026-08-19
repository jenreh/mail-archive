"""The knobs the parser has, and nothing else.

Deliberately tiny. Everything else about a mailbox — where it is, who it
belongs to, how often it syncs — is account state in SQLite, not settings.
"""

from appkit_commons.configuration.base import BaseConfig
from pydantic_settings import SettingsConfigDict


class MailConfig(BaseConfig):
    """How aggressively a message body is reduced before it is hashed.

    The defaults are what the analyses need; the switches exist so a debugging
    session can see the raw text a rule removed. Turning either off makes
    ``body_clean`` closer to ``body_text``, which is exactly the failure mode
    A3 was designed to avoid — a shared company footer then makes every
    message look like the same template.
    """

    model_config = SettingsConfigDict(
        env_prefix="app_mail_",
        env_file=".env",
        populate_by_name=True,
    )

    shingle_size: int = 3
    """Words per shingle for the SimHash. Three is the spec's figure."""

    strip_quotes: bool = True
    """Drop quoted predecessors: ``>`` lines and everything past a reply intro."""

    strip_signatures: bool = True
    """Drop sign-offs, ``--`` signature blocks and legal disclaimers."""
