"""Regenerate every ``.drawio`` and ``.svg`` under this directory.

    uv run python docs/diagrams/build.py

Build-time tooling. Nothing in the application imports it, and it needs nothing
installed beyond what the project already depends on.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from diagrams import ALL  # noqa: E402
from render import write  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    here = Path(__file__).parent
    for diagram in ALL:
        drawio, svg = write(diagram, here)
        logger.info("%s -> %s, %s", diagram.name, drawio.name, svg.name)
    logger.info("%d diagrams written to %s", len(ALL), here)


if __name__ == "__main__":
    main()
