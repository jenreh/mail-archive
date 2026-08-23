"""What the package promises the application, and what it promises not to do.

Two claims, and the second is the one the split was for. The surface has to be
reachable as ``from mailarc_mcp import ArchiveAccess, build_server`` — the entry
point under ``app/`` is three statements long and would stop being thin the
moment it had to know this component's layout. And **nothing here may name
``app``**: a component that reads the composition root cannot be installed
without it, which is precisely the state ``app/mcp_server/`` was in and the
reason the desktop bundle could not leave ``fastmcp`` behind.

The second claim is read off the source rather than off ``sys.modules``,
because an import that only happens down one branch would not show up in a
probe and would still make the package un-installable.
"""

import ast
from pathlib import Path

import mailarc_mcp

SOURCE = Path(mailarc_mcp.__file__).resolve().parent

FORBIDDEN = ("app", "mailarc_sync", "mailarc_google", "reflex", "runic.rag")
"""Everything §6's import table puts out of this component's reach.

``app`` is the composition root above it, ``mailarc_sync`` and
``mailarc_google`` are siblings it has no business knowing — a tool answers
questions about an archive that already exists, not about how mail got into one
— and the last two are the repository-wide bans.
"""


def _imported(path: Path) -> set[str]:
    """Every module name this file imports, dotted, wherever it sits."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_package_re_exports_what_the_entry_point_needs() -> None:
    """Three names and the console script is written: the access object it
    fills from the composition root, the builder it hands it to, and the
    logging fix a stdio process needs before it serves anything."""
    assert mailarc_mcp.__doc__, "the docstring is what the surface promises"
    assert {"ArchiveAccess", "build_server", "route_fastmcp_logging"} <= set(
        mailarc_mcp.__all__
    )
    assert callable(mailarc_mcp.build_server)


def test_every_exported_name_actually_resolves() -> None:
    """``__all__`` that names something absent is a broken star import."""
    missing = [name for name in mailarc_mcp.__all__ if not hasattr(mailarc_mcp, name)]

    assert missing == []


def test_no_module_in_the_package_reaches_above_or_beside_it() -> None:
    """The layering, read off the source instead of recalled.

    An import inside a function body counts here and would not count in an
    import probe: a package that reaches for ``app.composition`` on one branch
    is still a package that cannot be installed on its own.
    """
    offenders = {
        path.relative_to(SOURCE).as_posix(): sorted(names)
        for path in sorted(SOURCE.rglob("*.py"))
        for names in (
            {
                name
                for name in _imported(path)
                if name in FORBIDDEN
                or any(name.startswith(f"{one}.") for one in FORBIDDEN)
            },
        )
        if names
    }

    assert offenders == {}
