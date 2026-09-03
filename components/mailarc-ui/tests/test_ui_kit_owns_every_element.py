"""Every design element in this application is built in exactly one place.

``test_ui_kit_is_the_only_card`` made this argument for one element and it held
— the card stopped drifting the moment the fourteenth copy became impossible.
This is the same check widened to the rest of the vocabulary, and it was
written after counting what the widening was worth:

===================  =====  =========================================
element              sites  what the copies had already disagreed on
===================  =====  =========================================
``mn.alert``            21  teal vs green for a confirmation, a title
                            on four pages and none on three, ``py``
``mn.button``           18  four variants for three roles
``mn.badge``            11  ``tt="none"`` at four sites, so the same
                            status read ``idle`` and ``IDLE``
``mn.empty_state``       8  nothing yet — the copies were identical,
                            which is what makes the ninth cheap
``mn.progress``          5  a job row missing ``.ma-tabular`` on both
                            numbers, and one missing its status badge
``mn.loader``            5  ``py`` between ``lg`` and ``xl``, so the same
                            wait was a different height per panel
``mn.table``             3  two raw, one already ``kit.scroll_table``
===================  =====  =========================================

The rule this encodes is not "wrap everything". A ``mn.stack``, a ``mn.text``
and a ``mn.group`` are layout and typography and they stay where they are.
What belongs to the kit is what the *design* has exactly one of: one card, one
field, one alert, one badge, one button per role. A page reaching for the raw
component is a page deciding for itself what one of those looks like.

Read off the source with ``ast`` rather than a text search, for the reason the
card guard gives: ``status/components.py`` quotes ``mn.card(shadow="sm", …)``
in its module docstring to record what it stopped doing, and ``theme.py``
writes ``mn.button("Search")`` in a usage example. Neither is a call.
"""

import ast
from pathlib import Path

import mailarc_ui

PACKAGE = Path(mailarc_ui.__file__).parent
"""The installed sources, found through the import — see
``test_ui_kit_is_the_only_card`` for why not a relative path."""

KIT = PACKAGE / "kit"
"""The one package allowed to build any of these."""

OWNED: dict[str, str] = {
    "action_icon": "kit.pill_icon_action",
    "alert": "kit.message / kit.toned_message",
    "badge": "kit.status_badge / kit.dot_badge",
    "button": "kit.primary_button / soft_button / quiet_button / pill_action",
    "card": "kit.panel_card",
    "empty_state": "kit.empty_panel",
    "loader": "kit.spinner",
    "paper": "kit.attachment_card",
    "progress": "kit.job_progress / meter_bar / score_bar",
    "skeleton": "kit.placeholder_block",
    "table": "kit.scroll_table",
}
"""Each element the design has one of, and what a page should reach for.

The value is the failure message. A guard that only says "you may not do this"
sends somebody to read the kit's ``__init__``; one that names the function is
a guard people can act on without leaving the error.
"""


def _raw_elements(source: Path) -> set[str]:
    """Which ``mn.<element>(…)`` calls this module makes, namespaces included.

    ``mn.table.tr(…)`` is not a table — the sub-components are how a table's
    body is written, inside ``kit.scroll_table`` or out — so only the bare
    ``mn.table(…)`` counts. ``mn.alert_dialog.root(…)`` is likewise a dialog
    and not an alert, which is why this matches the whole callee rather than
    any attribute anywhere in it.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in OWNED
            and isinstance(func.value, ast.Name)
            and func.value.id == "mn"
        ):
            found.add(func.attr)
    return found


def test_no_module_outside_the_kit_builds_a_design_element() -> None:
    """One place decides what a card, an alert, a badge or a button is."""
    offenders = {
        str(path.relative_to(PACKAGE)): sorted(
            f"mn.{one} → {OWNED[one]}" for one in used
        )
        for path in PACKAGE.rglob("*.py")
        if KIT not in path.parents and (used := _raw_elements(path))
    }

    assert offenders == {}, f"these modules build their own elements: {offenders}"


def test_the_kit_is_where_each_one_is_actually_built() -> None:
    """The other half, and the one that catches a rule nothing implements.

    A guard that only forbids passes just as happily when the element is not
    drawn anywhere at all — including when somebody deletes the kit function
    and every call site with it. Each name has to still be built inside
    ``kit/``, by exactly the module that owns it.
    """
    built = set()
    for path in KIT.rglob("*.py"):
        built |= _raw_elements(path)

    assert built == set(OWNED), (
        f"the kit builds {sorted(built)}, but owns {sorted(OWNED)}"
    )
