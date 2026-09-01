"""What the package promises the application, and what it promises not to do.

Two claims, and the second is the one phase 5's definition of done spells out
in three words: *kein Embedder beteiligt*. The three analyses are exact — a
ticket token, a thread, a subject, an attachment hash, a participant hash, a
SimHash — and none of them may reach for a model. That is easy to honour while
writing the code and easy to lose the moment phase 6 adds one, so it is checked
by reading the imports rather than by remembering.

Phase 6 added one, which is what :data:`EXEMPT` is. The claim did not get
weaker, it got a boundary: ``semantic/`` is where a model is allowed and every
other module in the package still may not name one. That boundary is worth more
than the blanket ban was — a blanket ban over a package that contains an
embedder is a test that has to be deleted, and a deleted test protects nothing.
The top-level ``__init__`` is *not* exempt: the semantic surface is reached as
``from mailarc_analytics.semantic import …``, so the package's own docstring and
re-exports stay free of it and A1-A3 remain what a bare
``import mailarc_analytics`` gets you.
"""

import ast
from pathlib import Path

import mailarc_analytics

SOURCE = Path(mailarc_analytics.__file__).parent

SEMANTIC = (
    "embedding", "embedder", "semantic", "sentence_transformers",
    "ollama", "torch", "numpy", "openai", "runic.rag",
)  # fmt: skip
"""Names that would mean a model got involved.

``numpy`` is on the list although it is not a model: it arrives *with* one,
and its appearance in a package that only ever counts bits and compares
timestamps is the first sign that something semantic has been let in.
"""


EXEMPT = ("semantic/",)
"""The one directory a model is allowed in — phase 6's embedder and KNN.

A path prefix and not a module list, so a new file under ``semantic/`` is
covered by the same decision that let the package exist, while a new file
anywhere else is not.
"""

DETERMINISTIC = ("mailarc_analytics.derived", "mailarc_analytics.queries")
"""This package's own two model-free trees, by import prefix.

A name under one of these is not a dependency on anything: every file in both
is scanned by the tests below, so importing one cannot smuggle a model in — if
it named an embedder, *that file* would be the offender. The exemption exists
because :data:`SEMANTIC` matches on substrings and one of the catalogue's
statement modules is called ``embedding``: it holds the five statements the
embed job binds, no model within reach of it, and the ban would otherwise be
firing on a file name. ``mailarc_analytics.semantic`` does not start with
either prefix, so every way of reaching the embedder is still caught.
"""


def _reaches_for_a_model(name: str) -> bool:
    """Whether an imported name means a model got involved.

    A first-party name inside :data:`DETERMINISTIC` never does; anything
    carrying one of :data:`SEMANTIC`'s markers does.
    """
    if name.startswith(DETERMINISTIC):
        return False
    return any(marker in name.lower() for marker in SEMANTIC)


def _imported(path: Path) -> set[str]:
    """Every module name this file imports, dotted form, aliases resolved out.

    Upper-case names are left out of the dotted forms, because an imported
    constant is a *value* and not a dependency: ``queries/catalog.py`` holds a
    statement called ``WRITE_EMBEDDINGS``, and importing a string of Cypher is
    not what "no module reaches for a model" is about. The module part is kept
    unconditionally, so ``from mailarc_analytics import semantic`` and
    ``from mailarc_analytics.semantic import anything`` are both still caught.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if not alias.name.isupper()
            )
    return found


def test_the_package_re_exports_what_the_derive_job_needs() -> None:
    """``AnalyticsConfig`` plus ``rebuild_derived`` is the whole entry point.

    The composition root builds the one and the worker calls the other; if
    either has to be reached for through a submodule, the application ends up
    knowing the layout of a component instead of its surface.
    """
    assert mailarc_analytics.__doc__, "the docstring is what the surface promises"
    assert {"AnalyticsConfig", "rebuild_derived"} <= set(mailarc_analytics.__all__)
    assert callable(mailarc_analytics.rebuild_derived)


def test_every_exported_name_actually_resolves() -> None:
    """``__all__`` that names something absent is a broken star import."""
    missing = [
        name
        for name in mailarc_analytics.__all__
        if not hasattr(mailarc_analytics, name)
    ]

    assert missing == []


def test_no_module_in_the_package_imports_anything_semantic() -> None:
    """Phase 5's definition of done, read off the source rather than recalled.

    Every finding this package writes has to be defensible from a header or a
    hash. The moment an embedder appears, ``ABOUT.method`` stops meaning what
    §6.2 says it means — a fact rather than a suggestion — and no test that
    only checks the output would notice.
    """
    offenders = {
        path.relative_to(SOURCE).as_posix(): sorted(
            name for name in _imported(path) if _reaches_for_a_model(name)
        )
        for path in sorted(SOURCE.rglob("*.py"))
        if not path.relative_to(SOURCE).as_posix().startswith(EXEMPT)
    }

    assert {path: names for path, names in offenders.items() if names} == {}


def test_the_deterministic_analyses_are_the_ones_that_stay_clean() -> None:
    """Named directly, so the exemption above cannot quietly grow.

    ``derived/`` computes A1-A3 and ``queries/`` holds the statements they run.
    If either ever imports an embedder, ``ABOUT.method`` stops meaning what
    §6.2 says it means — a fact rather than a suggestion — and signal 6 stops
    being a thing a user can tell apart from the other five.
    """
    deterministic = [
        path
        for path in sorted(SOURCE.rglob("*.py"))
        if path.relative_to(SOURCE).as_posix().startswith(("derived/", "queries/"))
    ]
    offenders = {
        path.relative_to(SOURCE).as_posix(): sorted(
            name for name in _imported(path) if _reaches_for_a_model(name)
        )
        for path in deterministic
    }

    assert deterministic, "the two deterministic packages have to exist at all"
    assert {path: names for path, names in offenders.items() if names} == {}
