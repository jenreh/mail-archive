"""One card recipe in the application, and one place it is written.

The recipe — ``mn.card(shadow=…, padding="lg", radius="md", with_border=True,
w="100%")`` — was open-coded eleven times before ``kit.panel_card`` existed,
and the copies had already drifted on ``shadow``. A reviewer cannot catch the
twelfth by reading a diff, because a new card looks entirely reasonable on its
own; only a count across the package shows it.

So this reads the source rather than a render. ``ast`` and not a text search,
because ``status/components.py`` quotes the old recipe in its module docstring
to say what it stopped doing — a ``grep`` would call that a violation, and
deleting the sentence to appease the check would remove the only record of
why the module changed.

The same argument covers the two promoted privates. ``_stat`` and
``_card_heading`` are now ``kit.stat_tile`` and ``kit.card_heading``; a module
that keeps its own copy under the old name gets no error and quietly renders a
second design.
"""

import ast
from pathlib import Path

import mailarc_ui
from mailarc_ui.insights import components as insights_components

PACKAGE = Path(mailarc_ui.__file__).parent
"""The installed sources, found through the import rather than by a path
relative to this file — the component is installed as a workspace member and
the two directories are not required to stay in the same place."""

CARD_HOME = PACKAGE / "kit" / "card.py"
"""The one module allowed to call ``mn.card``."""

PROMOTED = ("_stat", "_card_heading")
"""What ``insights/components.py`` used to define and ``kit`` now owns."""


def _calls_mantine_card(source: Path) -> bool:
    """Whether this module calls ``mn.card`` anywhere.

    A docstring mentioning the call is not a call. Parsing is what tells the
    two apart.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "card"
            and isinstance(func.value, ast.Name)
            and func.value.id == "mn"
        ):
            return True
    return False


def test_only_the_kit_builds_a_card() -> None:
    """Every surface in the archive is a `kit.panel_card`."""
    offenders = sorted(
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if path != CARD_HOME and _calls_mantine_card(path)
    )

    assert offenders == [], (
        "these modules build their own card instead of using kit.panel_card: "
        f"{offenders}"
    )


def test_the_promoted_helpers_left_the_insights_page() -> None:
    """`kit` owns the stat tile and the card heading now.

    Keeping the private copies alongside the import is how the nine cards
    disagreed with each other in the first place.
    """
    leftovers = [name for name in PROMOTED if hasattr(insights_components, name)]

    assert leftovers == [], (
        f"insights/components.py still defines {leftovers}; kit owns them now"
    )
