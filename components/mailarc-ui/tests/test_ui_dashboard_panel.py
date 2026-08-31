"""What the dashboard actually draws — read off the render, not off the source.

Four claims that a state test cannot make and a reading of the module cannot
either, because all four are about what reaches the browser:

* the KPI band has somewhere to say why three of its tiles are dashed;
* the services checklist carries the one dotted divider §5c asks for;
* the checklist is **not** replaced by an alert, which is what would blank it
  out in the case §1.3 says it exists for;
* every colour on the page arrives as a ``--ma-*`` token, never as a literal.

A render is a nested dict of props and children, and a ``Var`` appears in it as
the state path it compiles to. So "is this var on the page" is a search of that
structure, and a var the components stopped using disappears from it — which is
exactly what happened to ``counts_error``: it was set on every failed read and
rendered nowhere.
"""

import json
import re
from typing import Any

from mailarc_ui.dashboard.components import (
    dashboard_panel,
    disk_card,
    messages_card,
    notifications_card,
    services_card,
    storage_card,
    system_card,
)

_LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")
"""A colour written out rather than named.

Both spellings, because a hex value and an ``rgba(…)`` are the same mistake:
a colour that cannot follow the colour scheme, cannot be re-tuned in one place
and is invisible to anyone reading the stylesheet.
"""


def _rendered(component: Any) -> str:
    """One component's whole render tree, as something searchable.

    ``default=str`` because a render holds ``Var`` objects and other things
    ``json`` has no opinion about; their ``repr`` is the state path, which is
    the part worth searching.
    """
    return json.dumps(component.render(), default=str)


def _components(node: Any, found: list[str] | None = None) -> list[str]:
    """Every Mantine component name in a render tree, in no order.

    Walks conditions as well as children: everything interesting on this page
    is behind an ``rx.cond``, and a rendered condition is a ``cond_state`` with
    a ``true_value`` and a ``false_value`` and no ``children`` at all.
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


class TestTheBandSaysWhyATileIsDashed:
    """``—`` is what a tile shows when nobody could read its number.

    It has no room for a sentence, so the sentence goes under the band. Without
    it a failed SQLite read left three tiles dashed and nothing on the page
    saying why — a state a reader cannot tell apart from an archive with no
    accounts and no users.
    """

    def test_the_counts_error_reaches_the_page(self) -> None:
        assert "counts_error" in _rendered(dashboard_panel())

    def test_it_waits_for_the_read_before_saying_anything(self) -> None:
        """An alert that flashed on every page load would be the page
        reporting a fault about itself."""
        assert "loading_counts" in _rendered(dashboard_panel())

    def test_every_panel_error_is_on_the_page(self) -> None:
        """Five strings for six panels; the checklist says it differently."""
        drawn = _rendered(dashboard_panel())

        for name in (
            "archive_error",
            "counts_error",
            "series_error",
            "storage_error",
            "notifications_error",
        ):
            assert name in drawn, f"{name} is set but never drawn"


class TestTheServicesChecklist:
    """§5c: one dotted divider, before the final group."""

    def test_it_draws_exactly_one_divider(self) -> None:
        assert _components(services_card().render()).count("Divider") == 1

    def test_the_divider_is_dotted(self) -> None:
        assert 'variant:\\"dotted\\"' in _rendered(services_card())

    def test_the_split_comes_from_the_state(self) -> None:
        """Not from arithmetic inside an ``rx.foreach``, which would be a
        subtraction over a ``Var`` whose length is not a Python number."""
        assert "services_split" in _rendered(services_card())

    def test_nothing_can_replace_the_checklist_with_an_alert(self) -> None:
        """The case §1.3 promises this card will explain is the case where its
        own read failed. An alert in its place would blank it out exactly
        then — so the card has no error branch at all, and a checklist row says
        "could not ask" by going grey instead.
        """
        drawn = _components(services_card().render())

        assert "Alert" not in drawn
        assert "Alert" in _components(notifications_card().render()), (
            "the comparison is only worth making while other cards do alert"
        )


class TestNoColourIsWrittenOutOnThePage:
    """The rule the stylesheet exists to enforce, checked where it can fail.

    ``tests/test_stylesheets.py`` proves that every ``var(--ma-…)`` the
    interface names is a token the sheet declares and vice versa — but a
    ``#f4f4f3`` dropped into a ``style=`` dict is invisible to it, because it
    names no token at all. That is the failure this catches: it renders
    correctly, in one colour scheme, until somebody switches to the other.
    """

    def test_no_colour_literal_reaches_the_render(self) -> None:
        found = _LITERAL_COLOUR.findall(_rendered(dashboard_panel()))

        assert found == [], f"colours written out rather than named: {found}"

    def test_both_statistics_cards_take_their_bars_from_a_token(self) -> None:
        """Mantine's ``color`` prop takes a CSS colour and not a class, so the
        filled half of a meter is the one thing on this page a component has to
        hand over itself. It hands over the token's name."""
        assert "var(--ma-meter-warm)" in _rendered(system_card())
        assert "var(--ma-meter-cool)" in _rendered(disk_card())

    def test_both_charts_draw_their_line_in_the_accent(self) -> None:
        for card in (messages_card(), storage_card()):
            assert "var(--ma-chart-line)" in _rendered(card)


class TestTheChartKeepsTheClassItsColoursHangFrom:
    """``.ma-panel-chart`` is not decoration — it is the only way in.

    Two rules in ``mail-archive.css`` reach inside the chart's SVG through
    that class: the one that recolours the gradient's top stop to
    ``--ma-chart-fill``, and the one that sizes the axis ticks recharts
    renders as bare ``<text>``. Both fail silently if the box loses the class —
    the area fades out of a washed-out line colour instead, which is a
    difference nobody sees without the reference beside them.
    """

    def test_the_chart_box_wears_it(self) -> None:
        assert "ma-panel-chart" in _rendered(messages_card())

    def test_the_chart_is_not_also_a_well(self) -> None:
        """``.ma-panel`` is the recessed tint a statistics card digs into its
        white; a chart sits on the card's own surface, where the hairline grid
        and the muted ticks were drawn to read."""
        drawn = _rendered(storage_card())

        assert "ma-panel " not in drawn
        assert '\\"ma-panel\\"' not in drawn
        assert "ma-panel" in _rendered(system_card()), (
            "the comparison is only worth making while another card is a well"
        )
