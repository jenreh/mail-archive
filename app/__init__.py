import logging

from dotenv import load_dotenv

from app.configuration import configure

logger = logging.getLogger(__name__)
logger.info("Configuring application...")

load_dotenv()

#: The loaded application settings. Named `settings`, not `configuration`, so
#: `from app import settings` cannot be confused with the `app.configuration`
#: module it comes from.
settings = configure()
