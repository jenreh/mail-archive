"""The pages that are a stack of cards lay them out in columns.

The dashboard learned this first and ``dashboard/components.py`` states the
rule: **columns, not a grid of rows** — a row is as tall as its tallest cell,
so a twelve-row listing beside a three-row one leaves a band of empty white as
deep as the difference. Two columns have nothing to align; each closes up and
the slack lands at the foot of the shorter one.

Insights and the graph status page came the other way round. Both were a
single centred column with a maximum width, which hid the question entirely:
at 900px there was only ever one card across. The pages fill the window now,
and a four-column table drawn across 1300px is mostly white — so both grew the
same two-column body, and these are the assertions that keep them there.

Read off the render rather than the source: a card moved into a column is
still imported, still called and still in the module, and only the tree says
where it ended up.
"""

from typing import Any

from mailarc_ui.insights.components import insights_panel
from mailarc_ui.status.components import status_panel


def _components(node: Any, found: list[str] | None = None) -> list[str]:
    """Every Mantine component name in a render tree, in no order.

    Walks conditions as well as children, the way the dashboard's own panel
    test does: every listing on the insights page is behind an ``rx.cond``,
    and a rendered condition is a ``cond_state`` with a ``true_value`` and a
    ``false_value`` and no ``children`` at all.
    """
    found = [] if found is None else found
    if not isinstance(node, dict):
        return found
    if isinstance(name := node.get("name"), str):
        found.append(name)
    for child in node.get("children", []):
        _components(child, found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _components(subtree, found)
    return found


class TestTheInsightsListingsSitInTwoColumns:
    """The cross-check across the width, the six listings beside each other.

    Three to a column, and which column a card is in is decided by how wide its
    widest row is: the listings whose rows are prose — a subject and its
    reasons, a topic and its keywords, a template's own text — take the wide
    one, and the three that are a name and a handful of numbers take the other.
    """

    def test_the_listings_are_laid_out_as_two_columns(self) -> None:
        drawn = _components(insights_panel().render())

        assert drawn.count("Grid") == 1, "more than one grid is more than one rule"
        assert drawn.count("Grid.Col") == 2, (
            "a third column is a row again, and the white comes back with it"
        )

    def test_every_card_is_still_on_the_page(self) -> None:
        """Rearranging is not dropping.

        Ten surfaces: the rebuild card, the totals, the cross-check, the six
        listings — pairs and groups, topics, templates, circles, what matters,
        and tags — and the guidance panel, which is the other branch of the
        same ``rx.cond`` the listings sit in and is therefore in the tree
        beside them rather than instead of them. A ``panel_card`` renders as a
        ``Card``, so a listing that fell out while the layout was being
        rearranged shows up here as a nine.
        """
        assert _components(insights_panel().render()).count("Card") == 10


class TestTheGraphStatusCardsSitInTwoColumns:
    """The connection across the top, the server beside the inventory."""

    def test_the_lower_cards_are_laid_out_as_two_columns(self) -> None:
        drawn = _components(status_panel().render())

        assert drawn.count("Grid") == 1
        assert drawn.count("Grid.Col") == 2

    def test_every_card_is_still_on_the_page(self) -> None:
        assert _components(status_panel().render()).count("Card") == 3
