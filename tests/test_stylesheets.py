"""Every stylesheet the application asks for is one the application ships.

``mailarc_ui.styles.base_stylesheets`` is a list of strings that Reflex hands
to the browser as ``<link>`` hrefs, resolved against ``assets/`` at the
repository root. A path in that list that names no file produces a 404 in the
network tab and nothing else — no import error, no failing render, no red test.
The whole design of the shell is a set of ``--ma-*`` custom properties in
``assets/css/mail-archive.css``, so that silent 404 is the difference between
the archive's own look and appkit's defaults, and it would reach a screenshot
before it reached anybody's attention.

Here rather than in ``components/mailarc-ui/tests/``, because the claim spans
two things and only the root knows both: the list lives in the component, and
``assets/`` lives beside ``app/`` — the component's own docstring is careful to
say that a name in this list is a path into that directory and not an import.
"""

import re
from pathlib import Path

import mailarc_ui
from mailarc_ui.styles import base_stylesheets

ROOT = Path(__file__).resolve().parent.parent
"""The repository root — where Reflex serves ``assets/`` from."""

ASSETS = ROOT / "assets"

ARCHIVE_SHEET = "css/mail-archive.css"
"""The archive's own design tokens."""

APPKIT_SHEET = "css/appkit.css"
"""What the tokens have to be able to overrule."""

UI_PACKAGE = Path(mailarc_ui.__file__).parent
"""The installed component sources, found through the import rather than by a
path relative to this file — the component is a workspace member and the two
directories are not required to stay in the same place."""

_DECLARED = re.compile(r"^\s*(--ma-[a-z0-9-]+)\s*:", re.MULTILINE)
_USED = re.compile(r"var\(\s*(--ma-[a-z0-9-]+)")


def _local(sheets: list[str]) -> list[str]:
    """The entries that name a shipped file rather than a remote font."""
    return [one for one in sheets if not one.startswith(("http://", "https://"))]


def test_every_local_stylesheet_named_is_a_file_that_exists() -> None:
    """The check that would have caught a rename or a move."""
    missing = sorted(
        one for one in _local(base_stylesheets) if not (ASSETS / one).is_file()
    )

    assert missing == [], (
        f"named in base_stylesheets but absent from assets/: {missing}"
    )


def test_the_archive_brings_its_own_stylesheet() -> None:
    """Dropping this entry costs every ``--ma-*`` token in the shell."""
    assert ARCHIVE_SHEET in base_stylesheets


def test_the_archive_stylesheet_is_loaded_after_appkits() -> None:
    """Order is the override.

    Both files define custom properties on ``:root``; the later link wins a
    tie. Loading the archive's first would leave the shell reading as appkit's
    default palette while the tokens sit in the stylesheet unused, which is a
    failure that looks exactly like nobody having written them.
    """
    assert base_stylesheets.index(ARCHIVE_SHEET) > base_stylesheets.index(APPKIT_SHEET)


def _sheet() -> str:
    return (ASSETS / ARCHIVE_SHEET).read_text(encoding="utf-8")


def _painted() -> set[str]:
    """Every ``--ma-*`` token something actually reads.

    Both halves of the interface, because a token is used from either: the
    stylesheet reads most of them, and the handful Mantine takes as a prop
    (``mn.progress(color=…)``) are named from a component instead.
    """
    found = set(_USED.findall(_sheet()))
    for source in (*UI_PACKAGE.rglob("*.py"), *(ROOT / "app").rglob("*.py")):
        found.update(_USED.findall(source.read_text(encoding="utf-8")))
    return found


def test_every_declared_token_is_one_the_interface_paints() -> None:
    """A token nobody reads is a colour the design was promised and never got.

    ``--ma-chart-fill`` was declared in both palettes and referenced nowhere:
    the area chart faded out of ``--ma-chart-line`` instead, and the only way
    to see it was to compare a screenshot against the brief. A stylesheet that
    advertises a colour it never paints is worse than one that omits it,
    because it reads as done.
    """
    declared = set(_DECLARED.findall(_sheet()))
    painted = _painted()

    assert declared - painted == set(), (
        "declared in mail-archive.css but read by nothing: "
        f"{sorted(declared - painted)}"
    )


def test_every_token_the_interface_paints_is_one_the_sheet_declares() -> None:
    """The other direction: a ``var(--ma-…)`` naming nothing resolves to
    nothing, and the property it was set on silently keeps its inherited
    value — a typo that renders as a design decision."""
    unknown = _painted() - set(_DECLARED.findall(_sheet()))

    assert unknown == set(), f"read but never declared: {sorted(unknown)}"
