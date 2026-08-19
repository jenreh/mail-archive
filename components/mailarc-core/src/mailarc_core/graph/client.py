"""Getting hold of the graph — through runic's protocols, not one client.

Graph data is read and written through a :class:`runic.ogm.Session` over a
:class:`runic.ogm.GraphDriver`. Neither name is FalkorDB's: which database
answers is :attr:`~mailarc_core.graph.config.GraphConfig.backend`'s decision,
made once here and nowhere else. The FalkorDB-only operations live next door
in :mod:`~mailarc_core.graph.admin`.

Kept apart from :mod:`~mailarc_core.graph.status` so the lifecycle code in
:mod:`~mailarc_core.graph.server` can probe a port without depending on
status reporting.
"""

import contextlib
import logging
from collections.abc import Iterator

from runic.ogm import GraphDriver, Session, create_driver

from mailarc_core.graph import admin
from mailarc_core.graph.config import GraphConfig

logger = logging.getLogger(__name__)


def connect(config: GraphConfig) -> GraphDriver:
    """Open a driver on the configured graph. Caller calls :func:`close`."""
    logger.debug(
        "Opening %s on %s via %s", config.graph_name, config.endpoint, config.backend
    )
    return create_driver(config.backend, **config.driver_options())


def close(driver: GraphDriver) -> None:
    """Release a driver and the connection underneath it.

    The second call is for FalkorDB alone and does nothing for anyone else;
    :func:`~mailarc_core.graph.admin.release` explains why it is needed.
    """
    driver.close()
    admin.release(driver)


@contextlib.contextmanager
def session(config: GraphConfig) -> Iterator[Session]:
    """A runic session on the configured graph, torn down with its driver.

    A ``Session`` tracks entities, not connections: it is handed a driver and
    never closes it, so ownership stops here or every session leaves one
    behind. It commits on a clean exit and rolls back on an exception —
    against a backend whose driver is a
    :class:`~runic.ogm.TransactionalGraphDriver` (Bolt, AGE) that is a real
    transaction; FalkorDB has none, so each statement is atomic on its own.
    """
    driver = connect(config)
    try:
        with Session(driver) as graph_session:
            yield graph_session
    finally:
        close(driver)
