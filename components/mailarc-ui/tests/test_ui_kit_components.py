"""Each new kit piece builds, and carries the class the stylesheet keys on.

Read off the render, like ``test_ui_dashboard_panel``: a kit component's whole
contract with the design is a class name — the colours live in
``assets/css/mail-archive.css`` — so "does this component wear its class" is
the test, and a component that dropped one would keep building and silently
render unstyled.
"""

import json
from typing import Any

from mailarc_ui.kit import (
    attachment_card,
    avatar_initials,
    count_chip,
    date_field,
    field_label,
    input_field,
    label_chip,
    list_row,
    pill_action,
    primary_button,
    quiet_button,
    relevance_chip,
    segmented_field,
    select_field,
)


def _rendered(component: Any) -> str:
    """One component's whole render tree, as something searchable."""
    return json.dumps(component.render(), default=str)


class TestFieldLabel:
    """The mono-uppercase line over every form control."""

    def test_label_and_hint_wear_their_classes(self) -> None:
        drawn = _rendered(field_label("Sender", hint="optional"))

        assert "ma-field-label" in drawn
        assert "ma-field-hint" in drawn
        assert "Sender" in drawn
        assert "optional" in drawn

    def test_the_hint_sits_across_from_the_label(self) -> None:
        assert "space-between" in _rendered(field_label("Sender", hint="opt"))

    def test_no_hint_renders_no_hint(self) -> None:
        assert "ma-field-hint" not in _rendered(field_label("Sender"))


class TestFields:
    """Every control is the filled variant wearing ``.ma-field``."""

    def test_input_field(self) -> None:
        drawn = _rendered(input_field(label="Sender"))

        assert "ma-field" in drawn
        assert "filled" in drawn
        assert "ma-field-label" in drawn

    def test_a_field_without_a_label_is_the_bare_control(self) -> None:
        drawn = _rendered(input_field(placeholder="Search"))

        assert "ma-field" in drawn
        assert "ma-field-label" not in drawn

    def test_select_field(self) -> None:
        drawn = _rendered(select_field(label="Account", data=["all"]))

        assert "ma-field" in drawn
        assert "filled" in drawn

    def test_date_field(self) -> None:
        drawn = _rendered(date_field(label="From"))

        assert "ma-field" in drawn
        assert "filled" in drawn

    def test_segmented_field_fills_its_row(self) -> None:
        drawn = _rendered(segmented_field(label="Mode", data=["all", "sent"]))

        assert "ma-field" in drawn
        assert "fullWidth:true" in drawn

    def test_a_callers_class_is_added_not_replaced(self) -> None:
        drawn = _rendered(input_field(class_name="mine"))

        assert "ma-field mine" in drawn


class TestButtons:
    def test_primary_button_is_the_filled_accent(self) -> None:
        drawn = _rendered(primary_button("Search"))

        assert "ma-btn-primary" in drawn
        assert "filled" in drawn

    def test_quiet_button(self) -> None:
        drawn = _rendered(quiet_button("Reset"))

        assert "ma-btn-quiet" in drawn
        assert "subtle" in drawn

    def test_pill_action_carries_its_glyph(self) -> None:
        """The icon lands as a Lucide component in ``leftSection``."""
        drawn = _rendered(pill_action("Download", icon="download"))

        assert "ma-pill-action" in drawn
        assert "leftSection" in drawn
        assert "LucideDownload" in drawn


class TestChips:
    def test_count_chip_is_a_pill_with_tabular_digits(self) -> None:
        drawn = _rendered(count_chip("paperclip", 2))

        assert "ma-chip-pill" in drawn
        assert "ma-chip-count" in drawn
        assert "ma-tabular" in drawn

    def test_label_chip_carries_its_dot(self) -> None:
        drawn = _rendered(label_chip("Work", color="teal.5"))

        assert "ma-chip-label" in drawn
        assert "ma-chip-dot" in drawn
        assert "Work" in drawn

    def test_relevance_chip_is_the_accent_variant(self) -> None:
        drawn = _rendered(relevance_chip("92%"))

        assert "ma-chip-relevance" in drawn
        assert "92%" in drawn


class TestListRow:
    def test_the_row_wears_its_class_and_its_selection_attribute(self) -> None:
        drawn = _rendered(list_row(selected=False))

        assert "ma-list-row" in drawn
        assert "data-selected" in drawn

    def test_selection_drives_the_attributes_value(self) -> None:
        """The stylesheet keys on ``[data-selected="true"]``, so the value
        must be a conditional over exactly those two spellings — a rendered
        cond carries both branches, and the condition is what flips."""
        assert '(true ? \\"true\\" : \\"false\\")' in _rendered(list_row(selected=True))
        assert '(false ? \\"true\\" : \\"false\\")' in _rendered(
            list_row(selected=False)
        )


class TestAttachmentCard:
    def test_it_is_the_bordered_white_card(self) -> None:
        drawn = _rendered(attachment_card("report.pdf", "123 KB · Download"))

        assert "ma-attachment-card" in drawn
        assert "ma-attachment-icon" in drawn
        assert "ma-attachment-name" in drawn
        assert "ma-attachment-meta" in drawn
        assert "report.pdf" in drawn
        assert "123 KB" in drawn


class TestAvatar:
    def test_initials_come_from_the_name_at_list_size(self) -> None:
        drawn = _rendered(avatar_initials("Ada Lovelace"))

        assert "Ada Lovelace" in drawn
        assert "initials" in drawn
        assert "size:36" in drawn

    def test_the_reading_pane_size_is_a_parameter(self) -> None:
        assert "size:44" in _rendered(avatar_initials("Ada Lovelace", size=44))
