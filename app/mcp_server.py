"""``mail-archive-mcp``: the process the MCP server runs in, and nothing more.

The composition root of its own process, the way ``app/worker.py`` is: it
configures logging, configures the application, builds the four readers out of
``app/composition.py`` and hands them to :func:`mailarc_mcp.build_server`. The
server itself — the six tools, their schemas, every sentence a failure puts on
the wire — is ``components/mailarc-mcp/``, which may not read a setting and so
cannot build any of this for itself (§4.1).

**Nothing else in ``app/`` may import this module.** ``mailarc-mcp`` is an
optional extra: ``uv sync`` resolves without it so that the desktop bundle does
not carry ``fastmcp``'s sixty distributions, and ``uv sync --extra mcp`` puts it
back for the web deployment. The web application and the worker have to start on
either, which they do as long as this file is reached only by the console script
that names it. ``tests/test_mcp_server.py`` reads the imports and checks.

The import of ``mailarc_mcp`` is therefore inside the functions that need it
rather than at the top: an installation without the extra still has the
``mail-archive-mcp`` script — entry points come out of the root wheel's metadata,
which knows nothing about extras — and running it should say which flag is
missing, not raise ``ModuleNotFoundError`` at a user.
"""

import logging
from contextlib import AbstractContextManager
from importlib import metadata
from typing import TYPE_CHECKING

from runic.ogm import Session

from app.composition import (
    analytics_reader,
    archive_reader,
    graph_config,
    semantic_search,
)
from app.configuration import configure
from mailarc_core.graph.client import session as graph_session

if TYPE_CHECKING:
    from mailarc_mcp import ArchiveAccess

logger = logging.getLogger(__name__)

NOT_INSTALLED = (
    "mail-archive-mcp needs the `mcp` extra, and this installation was built "
    "without it. Install it with `uv sync --extra mcp`. The desktop bundle "
    "leaves it out on purpose: the MCP server pulls in fastmcp, which is around "
    "sixty distributions a desktop archive never uses."
)
"""What a person gets instead of a traceback when the extra is absent.

The script is installed either way, because ``[project.scripts]`` is metadata on
the root wheel and has no notion of an extra. So the one thing this process must
not do is fail with an import error naming a package the reader has never heard
of.

**Reachable from a checkout, and — today — not from a wheel.** Measured: in a
venv built with ``uv pip install .``, ``<venv>/bin/mail-archive-mcp`` does not
get here at all. The shim's ``from app.mcp_server import main`` imports
``app/__init__.py`` first, which calls ``configure()`` at module scope, and
``[tool.hatch.build.targets.wheel] packages = ["app"]`` ships no
``configuration/`` — so the installed copy resolves a different configuration
directory and dies with ``SecretNotFoundError`` before this sentence can be
said. Not caused by the ``mcp`` extra and not fixed by it: ``python -P -c
'import app'`` behaves the same way in ``.venv.mac``, which works only because
``app`` there resolves to the checkout. Closing it means either shipping
``configuration/`` in the wheel or moving ``configure()`` out of
``app/__init__.py`` into the entry points that need it, and both are packaging
changes with their own startup path to prove. Stated here rather than left as a
claim this module cannot keep.
"""


def archive_access() -> ArchiveAccess:
    """The four readers a tool answers from, bound to this installation.

    Factories rather than objects, so that nothing is built here: a client lists
    an MCP server's tools before it calls one, and this process has to answer
    that against a machine whose graph is not running. Each of the four is a
    cached builder in :mod:`app.composition`, which is what keeps the answer to
    "is semantic search available" the same in this process and in the Reflex
    one — they call the same builder rather than each reading the same setting.
    """
    from mailarc_mcp import ArchiveAccess

    return ArchiveAccess(
        graph_session=_graph_session,
        analytics=analytics_reader,
        archive=archive_reader,
        search=semantic_search,
    )


def main() -> None:
    """Configure this process, then serve the archive over stdio.

    The same three steps ``app/worker.py`` takes — logging, configuration, run —
    plus one this process needs and the worker does not: FastMCP installs its own
    stderr handlers at import and stops its records propagating, so an unmasked
    traceback would be written by a logger this application does not control, in
    a format it did not choose, to a stream the MCP client captures.
    :func:`~mailarc_mcp.route_fastmcp_logging` puts those records back under this
    application's logging instead of leaving them to a second, invisible
    configuration.

    ``show_banner=False`` for two reasons. The banner is decoration next to a
    JSON-RPC stream, and rendering it makes an outbound HTTPS request to PyPI for
    a version check and writes a cache file — neither belongs in a private mail
    archive's startup.
    """
    logging.basicConfig(level=logging.INFO)
    try:
        from mailarc_mcp import build_server, route_fastmcp_logging
    except ModuleNotFoundError as error:
        # Only the two names this sentence actually answers for. The import
        # chain behind it reaches `fastmcp.exceptions` and `fastmcp.tools.base`
        # — an import the component's README calls out as fragile across
        # fastmcp releases — and any of those going missing is a different
        # fault with a different fix. Telling somebody who already has the
        # extra to install the extra costs them the one thread they had.
        if error.name not in {"mailarc_mcp", "fastmcp"}:
            raise
        raise SystemExit(NOT_INSTALLED) from error

    route_fastmcp_logging()
    # Importing `app` already configured the process; saying so here keeps this
    # server's dependency on configuration a statement rather than a side
    # effect. `configure` is cached, so this is free.
    configure()
    logger.info("Serving the mail archive over stdio")
    server = build_server(archive_access(), version=version())
    server.run(transport="stdio", show_banner=False)


def version() -> str:
    """This application's version, for the ``serverInfo`` a client displays.

    Read off the installed distribution rather than written down, because a
    second copy of a version number is a copy that drifts — and read *here*
    rather than in the component, because ``mail-archive`` is the name of the
    thing a person installed and a component may not know it. Left to itself
    FastMCP reports its own version, so a client would show the archive as 3.4.7
    and a bug report would name the wrong software.
    """
    try:
        return metadata.version("mail-archive")
    except metadata.PackageNotFoundError:  # pragma: no cover - always installed
        logger.debug("mail-archive is not installed as a distribution")
        return "0"


def _graph_session() -> AbstractContextManager[Session]:
    """One session against the configured graph, opened per call.

    A function and not ``partial(graph_session, graph_config())``, so that the
    configuration is read when a tool opens a session rather than when this
    module builds the access object. Building it must stay free of both: the
    tool listing happens first and must not need a registry, let alone a server.
    """
    return graph_session(graph_config())


if __name__ == "__main__":
    main()
