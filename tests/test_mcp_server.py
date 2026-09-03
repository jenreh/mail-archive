"""What the application wires into the MCP server, and what it must not.

The server itself lives in ``components/mailarc-mcp/`` and is tested there: the
tool names, the schemas, the sentence a failure puts on the wire. None of that
needs this application. What does is the other half — the five readers a tool
answers from, the version a client displays, the console script that starts the
process, and the one property the whole split exists for:

**``mailarc-mcp`` is an extra, and the application starts without it.**
``uv sync`` resolves without ``fastmcp``'s sixty distributions so the desktop
bundle carries none of them; ``uv sync --extra mcp`` puts them back for the web
deployment. That only holds while nothing under ``app/`` imports
``app/mcp_server.py`` at import time, and "nothing" is not a thing to remember
— :func:`test_nothing_else_in_the_application_imports_the_entry_point` reads
every module in ``app/`` and checks.

The readers are asserted by **identity** against :mod:`app.composition`, which
is the only way to show that the MCP process and the Reflex process cannot
diverge: they are not two objects built from the same setting, they are one
object built once.
"""

import ast
import logging
import sys
import tomllib
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from appkit_commons.registry import service_registry

from app import composition, mcp_server
from mailarc_analytics.semantic import SemanticConfig, SemanticProvider
from mailarc_core.graph.config import GraphConfig

MANIFEST = Path(__file__).resolve().parents[1] / "pyproject.toml"
APP = Path(mcp_server.__file__).resolve().parent
ENTRY_POINT = Path(mcp_server.__file__).resolve()

OPTIONAL = ("mailarc_mcp", "fastmcp")
"""What only the entry point may name at import time.

Both, not just ``mailarc_mcp``: importing ``fastmcp`` straight from a page or
the worker would reintroduce exactly the dependency the extra exists to remove,
and it would do it without ever mentioning the component.
"""


def _manifest() -> dict[str, Any]:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def _imported(path: Path) -> set[str]:
    """Every module name this file names in an import, dotted.

    The whole tree, not ``tree.body``. A module-level import does not have to
    be a top-level *statement*: one nested in ``if TYPE_CHECKING:``, in ``try:
    ... except ImportError:`` or under a ``sys.version_info`` test runs at
    import time exactly like an unindented one, and the scan that read only the
    top level could not see any of them.

    ``app/mcp_server.py`` is exempted by the caller's path check rather than by
    restricting the scan, which is what the restriction was really doing: the
    entry point reaches for ``mailarc_mcp`` inside its functions on purpose,
    and excluding that one file by name is both narrower and honest, while
    excluding *every* nested import let three shapes through everywhere.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_console_script_points_at_the_entry_point() -> None:
    """A wrong path here fails only on a user's machine, at the moment they
    wire the archive into a client — so it is asserted rather than trusted."""
    assert _manifest()["project"]["scripts"] == {
        "mail-archive-mcp": "app.mcp_server:main"
    }


def test_the_server_is_an_extra_and_not_a_dependency() -> None:
    """The point of the whole split, read off the manifest.

    ``fastmcp`` out of the root's dependencies and ``mailarc-mcp`` behind the
    ``mcp`` extra is what makes ``uv sync`` a desktop bundle and
    ``uv sync --extra mcp`` a web deployment. Putting either back would be a
    one-line change that nothing else in the repository would notice.
    """
    manifest = _manifest()

    assert manifest["project"]["optional-dependencies"]["mcp"] == ["mailarc-mcp"]
    assert not [
        one for one in manifest["project"]["dependencies"] if "fastmcp" in one
    ], "fastmcp belongs to the extra; a plain dependency puts it in every bundle"
    assert "mailarc-mcp" not in manifest["project"]["dependencies"]


def test_nothing_else_in_the_application_imports_the_entry_point() -> None:
    """The property that lets an installation leave the extra out.

    ``app/app.py`` and ``app/worker.py`` have to import on a machine where
    ``mailarc_mcp`` is not installed. One module-level ``import`` anywhere in
    ``app/`` — a page adding a link to the MCP docs, a state reaching for a tool
    name — would take both processes down on the exact installation the extra
    exists to produce, and would do it at startup with a ``ModuleNotFoundError``
    naming a package the reader never asked for.
    """
    offenders = {
        path.relative_to(APP).as_posix(): sorted(names)
        for path in sorted(APP.rglob("*.py"))
        if path != ENTRY_POINT
        for names in (
            {
                name
                for name in _imported(path)
                if name == "app.mcp_server"
                or name.startswith(("app.mcp_server.", *OPTIONAL))
                or name in OPTIONAL
            },
        )
        if names
    }

    assert offenders == {}


def test_the_entry_point_never_names_the_protocol_package() -> None:
    """``mcp`` is in the lock file only because ``fastmcp`` pulls it in.

    The component's own test makes this claim about its three modules; the
    entry point is the fourth file that could break it, and it is the one whose
    failure a user meets — a console script that dies at import shows an MCP
    client a blank error.
    """
    offenders = {
        name
        for name in _imported(ENTRY_POINT)
        if name == "mcp" or name.startswith("mcp.")
    }

    assert offenders == set()


def test_the_version_a_client_displays_is_this_applications() -> None:
    """Not FastMCP's, which is what a server reports when nobody says.

    A client showing the archive as 3.4.7 sends every bug report to the wrong
    project.
    """
    assert mcp_server.version() == _manifest()["project"]["version"]


class TestTheAccessTheApplicationBuilds:
    """The one ``ArchiveAccess`` an installed server actually uses.

    Every test in the component subclasses it and overrides every accessor, so
    the object the console script builds is only exercised here — including the
    single line that decides whether a deployed MCP server has semantic search
    at all. Nothing here opens a connection: an ``ArchiveAccess`` is documented
    as building nothing at construction, and these assertions are what keep
    that true.
    """

    def test_it_reads_through_the_composition_root_and_builds_nothing(self) -> None:
        """AGENTS §6: one module turns configuration into an object, and it is
        neither this one nor the component. Asserted by identity against the
        composition root, which is the only way to show that the MCP process
        and the Reflex process cannot hold different readers."""
        access = mcp_server.archive_access()

        assert access.analytics() is composition.analytics_reader()
        assert access.archive() is composition.archive_reader()
        assert access.search() is composition.semantic_search()
        assert access.tags() is composition.tag_store()

    def test_semantic_search_is_off_by_default_and_says_so(self) -> None:
        """``provider=none`` is the supported default, so the tool has to fail
        with the sentence naming the setting rather than answer with nothing."""
        assert mcp_server.archive_access().search().available is False

    def test_a_configured_embedder_reaches_the_server(self) -> None:
        """A YAML profile or an ``app_semantic_provider`` that names a provider
        has to arrive here, or the MCP process is permanently text-only whatever
        the user configured."""
        registry = service_registry()
        saved = registry.snapshot()
        registry.register_as(
            SemanticConfig, SemanticConfig(provider=SemanticProvider.OLLAMA)
        )
        composition.semantic_embedder.cache_clear()
        composition.semantic_search.cache_clear()
        try:
            assert mcp_server.archive_access().search().available is True
        finally:
            registry.restore(saved)
            composition.semantic_embedder.cache_clear()
            composition.semantic_search.cache_clear()

    def test_its_graph_session_is_the_configured_one(self, monkeypatch) -> None:
        """Read when a tool opens a session, never when the access is built.

        A client lists an MCP server's tools before it calls one, and this
        process has to answer that against a machine whose graph is down — so
        constructing the access must not touch the registry, and the assertion
        below is that the configuration is read on the *call*.

        The session is never entered: the point is which graph it would open,
        and entering one would need a server.
        """
        opened: list[GraphConfig] = []

        def fake_session(config: GraphConfig) -> Iterator[None]:
            opened.append(config)
            return iter(())

        monkeypatch.setattr(mcp_server, "graph_session", fake_session)
        access = mcp_server.archive_access()

        assert opened == [], "building the access read no configuration"
        access.session()

        assert opened == [composition.graph_config()]


class TestServingOverStdio:
    """:func:`app.mcp_server.main`, with the transport stubbed out.

    Running the real thing would park on stdin waiting for JSON-RPC frames, so
    what is asserted is the four decisions ``main`` makes before that: FastMCP's
    logging is put back under this application's, the process is configured, the
    server is built with the access and version from above, and it is served
    over **stdio with no banner** — the banner makes an outbound HTTPS request
    to PyPI on startup, which is not a thing a private mail archive does.
    """

    @pytest.fixture
    def served(self, monkeypatch) -> list[dict[str, Any]]:
        import mailarc_mcp

        served: list[dict[str, Any]] = []

        class FakeServer:
            def run(self, **kwargs: Any) -> None:
                served.append(kwargs)

        def fake_build(access: Any, *, version: str) -> FakeServer:
            served.append({"access": access, "version": version})
            return FakeServer()

        monkeypatch.setattr(mailarc_mcp, "build_server", fake_build)
        return served

    def test_it_builds_the_server_from_the_composition_root(self, served) -> None:
        mcp_server.main()

        built = served[0]
        assert built["version"] == mcp_server.version()
        assert built["access"].analytics() is composition.analytics_reader()

    def test_it_serves_over_stdio_without_the_banner(self, served) -> None:
        mcp_server.main()

        assert served[1] == {"transport": "stdio", "show_banner": False}

    def test_it_puts_fastmcps_logging_back_under_this_application(self, served) -> None:
        """Masking protects the wire, not the log stream — and for a stdio
        server the client owns the stream FastMCP writes to."""
        fastmcp_logger = logging.getLogger("fastmcp")
        fastmcp_logger.addHandler(logging.NullHandler())

        mcp_server.main()

        assert fastmcp_logger.handlers == []
        assert fastmcp_logger.propagate is True


class TestTheMissingExtraGuard:
    """What the console script says when the ``mcp`` extra is not installed.

    The script is installed either way — ``[project.scripts]`` is metadata on
    the root wheel and has no notion of an extra — so this guard is the whole
    of what stands between a user and a ``ModuleNotFoundError`` naming a
    package they have never heard of.
    """

    @staticmethod
    def _refusing(error: BaseException) -> Any:
        """A ``mailarc_mcp`` whose attributes raise *error* on first touch.

        PEP 562: ``from mailarc_mcp import build_server`` on a module with a
        ``__getattr__`` runs it, so this reproduces "the component is present
        and something in its import chain is not" without uninstalling
        anything.
        """
        faux = types.ModuleType("mailarc_mcp")

        def raising(name: str) -> Any:
            raise error

        # `setattr` and not an assignment: PEP 562's module `__getattr__` is a
        # module *attribute*, and a type checker reads a direct assignment as
        # trying to override the descriptor on ModuleType itself.
        setattr(faux, "__getattr__", raising)  # noqa: B010
        return faux

    def test_the_extra_being_absent_is_a_sentence_and_not_a_traceback(
        self, monkeypatch
    ) -> None:
        missing = ModuleNotFoundError(
            "No module named 'mailarc_mcp'", name="mailarc_mcp"
        )
        monkeypatch.setitem(sys.modules, "mailarc_mcp", self._refusing(missing))

        with pytest.raises(SystemExit) as caught:
            mcp_server.main()

        assert str(caught.value) == mcp_server.NOT_INSTALLED

    def test_a_different_missing_module_is_not_blamed_on_the_extra(
        self, monkeypatch
    ) -> None:
        """The guard may only answer for the condition it names.

        ``mailarc_mcp`` imports ``fastmcp.tools.base`` directly, and the
        component's own README calls that import fragile across fastmcp
        releases. Reporting its absence as "install the mcp extra" sends
        somebody who already has the extra to run ``uv sync --extra mcp``,
        watch nothing change, and be left with no thread to pull.
        """
        elsewhere = ModuleNotFoundError(
            "No module named 'fastmcp.tools.base'", name="fastmcp.tools.base"
        )
        monkeypatch.setitem(sys.modules, "mailarc_mcp", self._refusing(elsewhere))

        with pytest.raises(ModuleNotFoundError) as caught:
            mcp_server.main()

        assert caught.value.name == "fastmcp.tools.base"

    def test_fastmcp_itself_is_still_answered_for(self, monkeypatch) -> None:
        """``fastmcp`` absent *is* the extra absent — it is what the extra pulls in."""
        missing = ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
        monkeypatch.setitem(sys.modules, "mailarc_mcp", self._refusing(missing))

        with pytest.raises(SystemExit) as caught:
            mcp_server.main()

        assert str(caught.value) == mcp_server.NOT_INSTALLED


class TestTheImportScanSeesNestedImports:
    """:func:`_imported` reads the whole file, not only its top level.

    A module-level import does not have to be a top-level *statement*: one
    inside ``if TYPE_CHECKING:``, ``try: ... except ImportError:`` or
    ``if sys.version_info`` runs at import time just the same, and the first of
    those is the shape somebody reaches for when a page "optionally" wants a
    tool name. That is worse than the loud failure the two bans above prevent:
    with the extra present the page takes one branch and on exactly the
    installation the extra exists to produce it silently takes the other.
    """

    def test_an_import_inside_a_try_block_is_found(self, tmp_path) -> None:
        module = tmp_path / "sneaky.py"
        module.write_text(
            "try:\n    import fastmcp\nexcept ImportError:\n    fastmcp = None\n"
        )

        assert "fastmcp" in _imported(module)

    def test_an_import_inside_type_checking_is_found(self, tmp_path) -> None:
        module = tmp_path / "typed.py"
        module.write_text(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n    from mailarc_mcp import ArchiveAccess\n"
        )

        assert "mailarc_mcp" in _imported(module)
