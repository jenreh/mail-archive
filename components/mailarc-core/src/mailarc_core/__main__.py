"""Run just FalkorDB, without the web app.

    uv run python -m mailarc_core

Useful for poking at the graph with `redis-cli` or a notebook. Ctrl-C stops
the server; the process never leaves one behind.

Settings come from the environment (``app_graph_*``, and ``.env``); the mode is
forced to local, because starting a server someone else operates is not a
thing this command could do.
"""

import logging
import signal
import sys
from types import FrameType

from mailarc_core.graph import FalkorDBServer, GraphConfig, GraphServerMode

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s - %(message)s",
        stream=sys.stderr,
    )

    config = GraphConfig(mode=GraphServerMode.LOCAL)
    server = FalkorDBServer(config)
    try:
        server.start()
    except Exception as exc:
        logger.error("Could not start FalkorDB: %s", exc)
        return 1

    logger.info(
        "FalkorDB is up. Try: redis-cli -p %d GRAPH.LIST   (Ctrl-C to stop)",
        config.port,
    )

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %d, stopping FalkorDB", signum)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    signal.pause()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
