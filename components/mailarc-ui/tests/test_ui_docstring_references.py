"""Every ``:mod:`` cross-reference in ``mailarc_ui`` names a module that exists.

This package documents itself heavily and on purpose — the traps that cost a
day to find are written down where the next reader will hit them. Prose that
points somewhere is only worth writing while it still points *there*, and two
of these references had already rotted before anything looked: both named
``.jsx`` files as though they were Python modules, because a ``ViewerShell``
and a ``PdfViewer`` genuinely exist and simply do not exist in Python.

Nothing else in the gate can see this. ``ruff`` reads code and not prose, the
type checker has no opinion about a docstring, and there is no Sphinx build in
this repository to fail on a broken reference — the docstrings are read in an
editor, which is exactly where a wrong link is followed and found missing.

Scoped to ``:mod:`` and to first-party names. A ``:func:`` or a ``:class:``
would need the module imported to be checked, and importing every module in
``mailarc_ui`` to validate prose is a heavier thing than this is worth; a
module path is resolvable from the file system alone.
"""

from __future__ import annotations

import importlib.util
import re
from functools import cache
from pathlib import Path

import pytest

import mailarc_ui

ROOT = Path(mailarc_ui.__file__).parent

REFERENCE = re.compile(r":mod:`~?(mailarc_[a-z_]+(?:\.[a-z_][a-z0-9_]*)*)`")
"""A Sphinx module reference to a first-party package.

The ``~`` prefix — which renders as the last segment only — is part of the
syntax and not of the name, so it is dropped before the path is resolved.
"""

SOURCES = sorted(ROOT.rglob("*.py"))


@cache
def _resolves(dotted: str) -> bool:
    """Whether a dotted path names a module or package that is installed.

    The top-level package is located through the import system, so this works
    for every sibling component in the workspace without this test knowing the
    workspace's shape. Below that the path is walked on disk rather than
    imported: importing every referenced module would pull Reflex, a database
    and a graph client into a test whose only question is whether a path is
    spelled correctly.
    """
    top, _, rest = dotted.partition(".")
    try:
        spec = importlib.util.find_spec(top)
    except ImportError, ValueError:
        return False
    if spec is None or not spec.submodule_search_locations:
        return False
    for root in spec.submodule_search_locations:
        base = Path(root).joinpath(*rest.split(".")) if rest else Path(root)
        if (base / "__init__.py").is_file() or base.with_suffix(".py").is_file():
            return True
    return False


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_every_module_reference_resolves(source: Path) -> None:
    dangling = sorted(
        {
            dotted
            for dotted in REFERENCE.findall(source.read_text(encoding="utf-8"))
            if not _resolves(dotted)
        }
    )

    assert not dangling, (
        f"{source.relative_to(ROOT.parent)} points at modules that do not exist: "
        f"{dangling}. A `.jsx` sibling is not a module — name it in double "
        "backticks as a path instead."
    )


def test_the_check_would_notice_a_broken_reference() -> None:
    """The guard's own guard.

    A regex that matched nothing would pass every file in the package and say
    nothing about any of them, which is the way a test like this fails: not by
    going red, but by going quietly green forever.
    """
    assert REFERENCE.findall(":mod:`mailarc_ui.kit.viewer_shell`") == [
        "mailarc_ui.kit.viewer_shell"
    ]
    assert not _resolves("mailarc_ui.kit.viewer_shell")
    assert _resolves("mailarc_ui.kit.viewer")
    assert _resolves("mailarc_core.mail.model")
