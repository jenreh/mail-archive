"""What the search page actually draws — read off the render, not the source.

The same idiom as ``test_ui_dashboard_panel``: a render is a nested dict of
props and children, a ``Var`` appears in it as the state path it compiles to,
and so "does this control reach the browser wearing the right class, bound to
the right var" is a search of that structure. Every claim below is one a state
test cannot make and a reading of the module cannot either.

Three of them are worth naming. A field that lost ``.ma-field`` keeps working
and renders unstyled, which no other test would notice. A ``disabled`` that
stopped following the chosen path leaves a form offering to narrow a search
that cannot be narrowed. And the Search button's ``disabled`` is the negation
of a computed var — if it ever became a literal, the button would go dead, or
live, for everybody.

Literal conditions are followed rather than searched through. ``rx.cond``
renders *both* branches whatever the condition is, so a row without a
relevance still carries the relevance chip in its dead branch; :func:`_taken`
drops the branch a constant condition did not choose, which is what makes
"this row shows no score" assertable at all.
"""

import json
from typing import Any

import pytest

from mailarc_ui.message_detail.model import LabelChip
from mailarc_ui.search.components import (
    _chips,
    _result_row,
    _sender_line,
    result_list,
    search_panel,
)
from mailarc_ui.search.form import ATTACHMENT_SEGMENTS, search_form
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    ResultRow,
)
from mailarc_ui.search.state import MailSearchState

FIELD_CLASS = 'className:\\"ma-field\\"'
"""How a kit field's hook appears in a rendered prop list.

The quotes are escaped because :func:`_drawn` dumps the tree as JSON — the
render itself holds the prop as ``className:"ma-field"``.
"""

FIELDS = 8
"""Search, Mode, From, To, the two dates, Attachments, Account."""

DISABLED_BY_PATH = 6
"""How many of them the semantic path switches off — everything but the
question and the path selector itself."""

SEMANTIC_NOTE = "the sender, date, attachment and account fields do not narrow it"
"""The ASCII tail of :data:`SEMANTIC_IS_TEXT_ONLY`.

Its own constant rather than the sentence itself, because Reflex emits a
string literal with every non-ASCII character escaped — the em dash reaches
the render as ``\\u2014``, and the whole sentence is therefore not a
substring of its own render.
"""

PAPERCLIP = "LucidePaperclip"
"""``rx.icon("paperclip")`` as the render spells it."""


def _taken(node: Any) -> Any:
    """One render tree with every constant condition already resolved.

    ``rx.cond`` keeps both branches, so a search of the raw tree finds what a
    literal ``False`` chose not to draw. A condition that compiled to ``true``
    or ``false`` is decided here; anything referring to a state var is left
    alone, because at render time it genuinely is both.
    """
    if isinstance(node, list):
        return [_taken(one) for one in node]
    if not isinstance(node, dict):
        return node
    if (state := node.get("cond_state")) in ("true", "false"):
        return _taken(node["true_value" if state == "true" else "false_value"])
    return {key: _taken(value) for key, value in node.items()}


def _drawn(component: Any) -> str:
    """One component's whole render tree, as something searchable.

    ``ensure_ascii=False`` so that a sentence with an em dash in it is still
    findable by the sentence, and ``default=str`` because a render holds
    ``Var`` objects whose ``repr`` is the state path worth searching.
    """
    return json.dumps(_taken(component.render()), default=str, ensure_ascii=False)


def _props(node: Any, found: list[str] | None = None) -> list[str]:
    """Every rendered prop in a tree, conditions walked too."""
    found = [] if found is None else found
    if isinstance(node, list):
        for one in node:
            _props(one, found)
        return found
    if not isinstance(node, dict):
        return found
    found.extend(one for one in node.get("props", []) if isinstance(one, str))
    _props(node.get("children", []), found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _props(subtree, found)
    return found


def _names(node: Any, found: list[str] | None = None) -> list[str]:
    """Every component name in a render tree, conditions walked too."""
    found = [] if found is None else found
    if isinstance(node, list):
        for one in node:
            _names(one, found)
        return found
    if not isinstance(node, dict):
        return found
    if isinstance(name := node.get("name"), str):
        found.append(name)
    _names(node.get("children", []), found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _names(subtree, found)
    return found


def _texts(component: Any) -> int:
    """How many ``Text`` nodes a component actually draws."""
    return _names(_taken(component.render())).count("Text")


def row(**overrides: Any) -> ResultRow:
    """One result row, as the state hands it to the list."""
    fields: dict[str, Any] = {
        "id": "m1@example.com",
        "sender": "Anna Bauer",
        "initials": "A B",
        "sender_address": "anna@example.com",
        "subject": "Rechnung 2026",
        "preview": "Die Rechnung liegt bei.",
        "when_label": "9m",
        "labels": [LabelChip(text="Kunden", color="blue")],
    }
    return ResultRow(**{**fields, **overrides})


class TestEveryFieldIsAKitField:
    def test_every_control_wears_the_class_the_stylesheet_keys_on(self) -> None:
        """The fill, the hairline and the coral focus ring are all that class."""
        assert _drawn(search_form()).count(FIELD_CLASS) == FIELDS

    def test_every_control_carries_a_mono_uppercase_label(self) -> None:
        assert _drawn(search_form()).count("ma-field-label") == FIELDS

    def test_the_two_dates_share_one_row(self) -> None:
        """``simple_grid(cols=2)`` is what the design draws them in."""
        assert "SimpleGrid" in _names(search_form().render())

    def test_the_question_is_the_only_field_the_search_icon_marks(self) -> None:
        assert _drawn(search_form()).count("LucideSearch") == 1

    def test_enter_in_the_question_box_searches(self) -> None:
        """A search form whose Enter key does nothing is a form people fight."""
        assert "search_on_enter" in _drawn(search_form())


class TestTheTwoPaths:
    def test_the_segments_come_from_the_state_rather_than_the_form(self) -> None:
        """Only the state knows whether an embedder exists."""
        assert "mode_options" in _drawn(search_form())

    def test_the_semantic_segment_is_dead_until_an_embedder_exists(self) -> None:
        """An enabled control over a path that always fails is a promise."""
        state = MailSearchState()

        assert state.mode_options[1]["disabled"] is True

        state.semantic_ready = True

        assert state.mode_options[1]["disabled"] is False

    def test_the_structured_half_follows_the_chosen_path(self) -> None:
        disabled = [
            one
            for one in _props(search_form().render())
            if one.startswith("disabled:") and "semantic_chosen" in one
        ]

        assert len(disabled) == DISABLED_BY_PATH

    def test_the_form_says_what_the_semantic_path_ignores(self) -> None:
        """Disabling the fields is not enough — a form that quietly stops
        honouring what is typed in it is how a search lies."""
        assert SEMANTIC_NOTE in _drawn(search_form())

    def test_the_attachment_segment_has_all_three_positions(self) -> None:
        assert [one["value"] for one in ATTACHMENT_SEGMENTS] == [
            ATTACH_ANY,
            ATTACH_WITH,
            ATTACH_WITHOUT,
        ]


class TestTheButtons:
    def test_search_is_the_one_accent_button(self) -> None:
        drawn = _drawn(search_form())

        assert drawn.count("ma-btn-primary") == 1
        assert "ma-btn-quiet" in drawn

    def test_search_is_dead_exactly_when_the_state_says_so(self) -> None:
        """The negation of a computed var, so the rule lives in one place."""
        assert "can_search" in _drawn(search_form())

    def test_search_shows_that_it_is_working(self) -> None:
        assert "searching" in _drawn(search_form())

    def test_reset_asks_the_state_to_start_over(self) -> None:
        assert "reset_form" in _drawn(search_form())


class TestWhatTheFormSaysBack:
    def test_an_error_is_red(self) -> None:
        assert 'c:\\"red.7\\"' in _drawn(search_form())

    def test_both_slots_are_on_the_page(self) -> None:
        drawn = _drawn(search_form())

        assert ".error" in drawn
        assert ".notice" in drawn


class TestAResultRow:
    def test_it_is_a_selectable_kit_row(self) -> None:
        drawn = _drawn(_result_row(row()))

        assert "ma-list-row" in drawn
        assert "data-selected" in drawn
        assert "select" in drawn

    def test_it_shows_the_sender_the_time_and_the_subject(self) -> None:
        drawn = _drawn(_result_row(row()))

        assert "Anna Bauer" in drawn
        assert "9m" in drawn
        assert "Rechnung 2026" in drawn
        assert "Die Rechnung liegt bei." in drawn

    def test_the_initials_are_the_archive_s_own(self) -> None:
        """Handed over as two words — see ``initials_of``."""
        assert "A B" in _drawn(_result_row(row()))

    def test_a_row_with_no_date_shows_no_separator(self) -> None:
        """The dot sits between two facts; with only one it would dangle.

        Counted rather than searched for, because a middot reaches the render
        escaped — see :data:`SEMANTIC_NOTE`.
        """
        assert _texts(_sender_line(row())) == 3
        assert _texts(_sender_line(row(when_label=""))) == 1

    def test_the_preview_is_one_line(self) -> None:
        assert 'truncate:\\"end\\"' in _drawn(_result_row(row()))


class TestTheChipsUnderARow:
    def test_a_row_with_files_wears_the_paperclip(self) -> None:
        assert PAPERCLIP in _drawn(_chips(row(has_attachments=True)))

    def test_a_row_without_them_wears_nothing(self) -> None:
        assert PAPERCLIP not in _drawn(_chips(row()))

    def test_the_count_is_shown_where_the_archive_knows_one(self) -> None:
        drawn = _drawn(_chips(row(has_attachments=True, attachment_count=3)))

        assert PAPERCLIP in drawn
        assert "3" in drawn

    def test_labels_are_pills_behind_their_own_colour(self) -> None:
        drawn = _drawn(_chips(row()))

        assert "Kunden" in drawn
        assert "ma-chip-dot" in drawn

    def test_a_ranked_hit_wears_its_score(self) -> None:
        drawn = _drawn(_chips(row(relevance_label="92%")))

        assert "ma-chip-relevance" in drawn
        assert "92%" in drawn

    def test_a_structural_match_wears_none(self) -> None:
        """A filtered listing matches or it does not; a score would be
        a ranking nobody computed."""
        assert "ma-chip-relevance" not in _drawn(_chips(row()))


class TestTheResultColumn:
    def test_it_says_how_many_of_how_many(self) -> None:
        assert "count_label" in _drawn(result_list())

    @pytest.mark.parametrize("sentence", ["Nothing matched", "Nothing archived yet"])
    def test_both_empty_lists_are_on_the_page(self, sentence: str) -> None:
        """They mean opposite things: an answer, and a state."""
        assert sentence in _drawn(result_list())

    def test_more_can_be_asked_for(self) -> None:
        assert "load_more" in _drawn(result_list())


class TestThePanel:
    def test_it_is_three_columns_in_one_bordered_frame(self) -> None:
        drawn = _drawn(search_panel())

        assert FIELD_CLASS in drawn  # the form
        assert "ma-list-row" in drawn  # the results
        assert "Pick a message" in drawn  # the reading pane

    def test_the_reading_pane_is_bound_to_this_page_s_state(self) -> None:
        """The shared pane takes the concrete state class, which is what lets
        the search and the review each keep their own open message."""
        assert "mail_search_state.tab" in _drawn(search_panel())
