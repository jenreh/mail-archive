"""Each new kit piece builds, and carries the class the stylesheet keys on.

Read off the render, like ``test_ui_dashboard_panel``: a kit component's whole
contract with the design is a class name — the colours live in
``assets/css/mail-archive.css`` — so "does this component wear its class" is
the test, and a component that dropped one would keep building and silently
render unstyled.
"""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
import reflex as rx

from mailarc_ui.kit import (
    FIELD_GAP,
    LABEL_GAP,
    VISIBLE_ROWS,
    MessageTone,
    attachment_card,
    avatar_initials,
    count_chip,
    date_field,
    docx_viewer,
    dot_badge,
    empty_panel,
    field_label,
    field_note,
    graph_canvas,
    group_chevron,
    group_header,
    input_field,
    job_progress,
    label_chip,
    list_row,
    message,
    meter_bar,
    multi_select_field,
    number_field,
    password_field,
    pdf_viewer,
    pill_action,
    pill_icon_action,
    placeholder_block,
    pptx_viewer,
    primary_button,
    quiet_button,
    range_select,
    range_switch,
    relevance_chip,
    score_bar,
    scroll_table,
    segmented_field,
    select_field,
    soft_button,
    spinner,
    status_badge,
    toast,
    toned_message,
    viewer_assets,
    xlsx_viewer,
)
from mailarc_ui.kit.toast import DURATION_MS, LEVELS, POSITION
from mailarc_ui.kit.viewer_assets import VIEWER_LIBRARIES, VIEWER_PACKAGES
from mailarc_ui.search.state import MailSearchState

TONES: tuple[MessageTone, ...] = ("failure", "warning", "success", "note")
"""The four every tone table in the kit is checked against."""

POINTER_EVENTS = r"rightSectionPointerEvents:\"all\""
"""How that prop reads in a dumped render — the quotes are JSON's escaping."""

_HANDLER = MailSearchState
"""A real state to bind an event and a value to.

The kit takes handlers rather than flags, so a test of one needs a state that
actually has them; the search page's is the one that uses these two.
"""


def _rendered(component: Any) -> str:
    """One component's whole render tree, as something searchable."""
    return json.dumps(component.render(), default=str)


def _script(event: Any) -> str:
    """The JavaScript a toast event runs — where its level and sentence are."""
    return " ".join(str(value) for _, value in event.args)


def _class_of(component: Any) -> str:
    """The one ``className`` prop a kit primitive puts on its root."""
    return next(
        prop for prop in component.render()["props"] if prop.startswith("className:")
    )


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

    def test_a_hint_can_be_a_control_rather_than_a_sentence(self) -> None:
        """The slot's second job: a switch that says how to read the field
        under it belongs across from its label, not in a row of its own. It is
        drawn as it stands — no ``ma-field-hint``, because it is not text."""
        drawn = _rendered(field_label("To", hint=range_switch(data=["all", "any"])))

        assert "ma-range" in drawn
        assert "ma-field-hint" not in drawn

    def test_a_hint_the_browser_decides_is_drawn_conditionally(self) -> None:
        """A generated field marks itself required, and only the provider that
        declared it knows whether it is — so the hint arrives as a ``Var`` and
        the empty case has to be decided in the browser, not here."""
        drawn = _rendered(field_label("Host", hint=rx.cond(True, "required", "")))

        assert "ma-field-hint" in drawn
        assert "cond" in drawn


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

    def test_multi_select_field(self) -> None:
        drawn = _rendered(multi_select_field(label="To", data=["bob@example.com"]))

        assert "ma-field" in drawn
        assert "filled" in drawn
        assert "MultiSelect" in drawn

    def test_date_field(self) -> None:
        drawn = _rendered(date_field(label="From"))

        assert "ma-field" in drawn
        assert "filled" in drawn

    def test_segmented_field_fills_its_row(self) -> None:
        drawn = _rendered(segmented_field(label="Mode", data=["all", "sent"]))

        assert "ma-field" in drawn
        assert "fullWidth:true" in drawn

    def test_password_field(self) -> None:
        drawn = _rendered(password_field(label="API key"))

        assert "ma-field" in drawn
        assert "filled" in drawn
        assert "PasswordInput" in drawn

    def test_number_field(self) -> None:
        drawn = _rendered(number_field(label="Dimension"))

        assert "ma-field" in drawn
        assert "filled" in drawn
        assert "NumberInput" in drawn

    def test_a_callers_class_is_added_not_replaced(self) -> None:
        drawn = _rendered(input_field(class_name="mine"))

        assert "ma-field mine" in drawn

    def test_a_callers_variant_still_wins(self) -> None:
        """``filled`` is the recipe's default, not a rule the kit enforces."""
        assert "unstyled" in _rendered(input_field(variant="unstyled"))


class TestClearingAtextBox:
    """What Mantine gives a Select through ``clearable`` and a text box not at
    all: one gesture that empties the field."""

    def test_a_box_with_no_handler_wears_no_button(self) -> None:
        assert "CloseButton" not in _rendered(input_field(label="Search", value="x"))

    def test_the_button_is_mantine_s_own_in_the_right_section(self) -> None:
        """``pointer-events`` included: Mantine's right section ignores clicks
        by default, and a clear button that cannot be clicked is worse than
        none — it looks like the feature is there and broken."""
        drawn = _rendered(
            input_field(label="Search", value="x", on_clear=_HANDLER.clear_query)
        )

        assert "CloseButton" in drawn
        assert POINTER_EVENTS in drawn

    def test_it_is_drawn_only_while_there_is_something_to_clear(self) -> None:
        """A control that is always there and does nothing half the time reads
        as broken. Read off the compiled condition, because ``rx.cond`` keeps
        both branches: a constant value decides which one runs."""
        empty = _rendered(input_field(value="", on_clear=_HANDLER.clear_query))
        filled = _rendered(input_field(value="x", on_clear=_HANDLER.clear_query))

        assert "(false ? " in empty
        assert "(true ? " in filled


class TestTheNoteUnderAControl:
    """A long explanation is one quiet line below the box, never a second
    caption above it — Mantine's own ``description`` would draw that."""

    def test_a_description_lands_under_the_control(self) -> None:
        drawn = _rendered(input_field(label="Model", description="The default."))

        assert "The default." in drawn
        assert "dimmed" in drawn
        assert "description:" not in drawn

    def test_the_note_is_the_last_thing_in_the_stack(self) -> None:
        names = [
            child["name"]
            for child in input_field(label="Model", description="x").render()[
                "children"
            ]
        ]

        assert names == ["Group", "TextInput", "Text"]

    def test_a_field_with_no_note_stacks_nothing_extra(self) -> None:
        assert len(input_field(label="Model").render()["children"]) == 2

    def test_a_bare_control_with_a_note_still_gets_a_stack(self) -> None:
        """No label is a legitimate field; a dropped note would not be."""
        drawn = _rendered(input_field(description="What it does."))

        assert "What it does." in drawn
        assert "ma-field-label" not in drawn

    def test_field_note_is_the_one_spelling_of_a_quiet_line(self) -> None:
        drawn = _rendered(field_note("Only OpenAI needs one."))

        assert "dimmed" in drawn
        assert "Only OpenAI needs one." in drawn


class TestTheFormGaps:
    """Both live in the kit because every form in the archive is spaced
    alike; when they lived in ``search/form.py`` no other form could reach
    them, and each new one picked a Mantine token of its own."""

    def test_a_control_sits_closer_to_its_label_than_to_the_next_field(
        self,
    ) -> None:
        assert LABEL_GAP < FIELD_GAP

    def test_the_label_gap_is_what_a_field_stacks_at(self) -> None:
        assert f"gap:{LABEL_GAP}" in _rendered(input_field(label="Model"))


class TestButtons:
    def test_primary_button_is_the_filled_accent(self) -> None:
        drawn = _rendered(primary_button("Search"))

        assert "ma-btn-primary" in drawn
        assert "filled" in drawn

    def test_quiet_button(self) -> None:
        drawn = _rendered(quiet_button("Reset"))

        assert "ma-btn-quiet" in drawn
        assert "subtle" in drawn

    def test_soft_button_is_the_tint_between_the_other_two(self) -> None:
        drawn = _rendered(soft_button("Cancel", color="red"))

        assert "ma-btn-soft" in drawn
        assert "light" in drawn
        assert "red" in drawn

    def test_the_four_roles_are_four_different_classes(self) -> None:
        """The whole point of having four functions: a page cannot land two
        roles on the same class by passing a different ``variant``."""
        classes = {
            _class_of(primary_button("a")),
            _class_of(soft_button("b")),
            _class_of(quiet_button("c")),
            _class_of(pill_action("d")),
        }

        assert len(classes) == 4

    def test_pill_action_carries_its_glyph(self) -> None:
        """The icon lands as a Lucide component in ``leftSection``."""
        drawn = _rendered(pill_action("Download", icon="download"))

        assert "ma-pill-action" in drawn
        assert "leftSection" in drawn
        assert "LucideDownload" in drawn

    def test_the_icon_pill_wears_the_pill_and_drops_the_words(self) -> None:
        """Same chrome, glyph alone — the label is not drawn as button text."""
        drawn = _rendered(pill_icon_action(icon="download", label="Download"))

        assert "ma-pill-action" in drawn
        assert "LucideDownload" in drawn
        assert "leftSection" not in drawn

    def test_the_icon_pill_keeps_its_label_where_it_can_be_read(self) -> None:
        """A glyph nobody can name is not a button: the words become the
        tooltip and the accessible name."""
        drawn = _rendered(pill_icon_action(icon="download", label="Download"))

        assert "Tooltip" in drawn
        assert "aria-label" in drawn
        assert drawn.count("Download") >= 2


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


class TestTheGroupChevron:
    def test_it_wears_its_class_and_its_open_attribute(self) -> None:
        drawn = _rendered(group_chevron(expanded=True))

        assert "ma-group-chevron" in drawn
        assert "data-expanded" in drawn

    def test_being_open_drives_the_attributes_value(self) -> None:
        """The stylesheet turns it on ``[data-expanded="false"]``, so the value
        must be a conditional over exactly those two spellings — the same
        contract ``list_row`` keeps for selection."""
        assert '(true ? \\"true\\" : \\"false\\")' in _rendered(
            group_chevron(expanded=True)
        )
        assert '(false ? \\"true\\" : \\"false\\")' in _rendered(
            group_chevron(expanded=False)
        )


class TestTheGroupHeader:
    def test_it_wears_its_class_and_its_open_attribute(self) -> None:
        drawn = _rendered(group_header("Anna Bauer", 4, expanded=True))

        assert "ma-group-header" in drawn
        assert "ma-group-chevron" in drawn
        assert "Anna Bauer" in drawn
        assert '(true ? \\"true\\" : \\"false\\")' in drawn

    def test_it_is_not_a_list_row(self) -> None:
        """Nothing opens behind it, so it must not look like something does."""
        assert "ma-list-row" not in _rendered(group_header("Anna", 1))


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


class TestScrollTable:
    """A listing shows a window of rows and keeps its column names on screen."""

    def test_the_box_around_it_is_what_scrolls(self) -> None:
        drawn = _rendered(scroll_table())

        assert "ma-table-scroll" in drawn

    def test_it_is_twelve_rows_high_by_default(self) -> None:
        assert VISIBLE_ROWS == 12
        assert "calc(12 * var(--ma-table-row) + var(--ma-table-head))" in _rendered(
            scroll_table()
        )

    def test_a_caller_can_ask_for_another_number_of_rows(self) -> None:
        assert "calc(5 * var(--ma-table-row)" in _rendered(scroll_table(rows=5))

    def test_the_header_is_pinned(self) -> None:
        """Mantine's own sticky head, against the box above as its scroller.
        Without it a reader who scrolls a ranking loses the column names and
        every number below becomes an unlabelled figure."""
        assert "stickyHeader" in _rendered(scroll_table())

    def test_the_table_recipe_arrives_with_it(self) -> None:
        """Striped, hover-highlighted, tabular numerals — stated once here
        rather than at each call site, which is where the copies drifted."""
        drawn = _rendered(scroll_table())

        assert "striped" in drawn
        assert "highlightOnHover" in drawn
        assert "tabularNums" in drawn

    def test_a_caller_still_overrides_the_recipe(self) -> None:
        assert "striped:true" in _rendered(scroll_table())
        assert "striped:false" in _rendered(scroll_table(striped=False))


class TestMessages:
    """Four tones, and a call site that picks a kind rather than a colour."""

    def test_each_tone_has_its_own_colour_and_glyph(self) -> None:
        seen = {
            tone: _rendered(message("something", tone))
            for tone in ("failure", "warning", "success", "note")
        }

        assert 'color:\\"red\\"' in seen["failure"]
        assert "LucideTriangleAlert" in seen["failure"]
        assert 'color:\\"yellow\\"' in seen["warning"]
        assert 'color:\\"green\\"' in seen["success"]
        assert "LucideCircleCheck" in seen["success"]
        assert 'color:\\"blue\\"' in seen["note"]
        assert "LucideInfo" in seen["note"]

    def test_every_message_is_the_light_variant(self) -> None:
        """The one thing all four share, and the prop seven modules were
        passing by hand."""
        for tone in ("failure", "warning", "success", "note"):
            assert "light" in _rendered(message("x", tone))

    def test_a_remark_is_tighter_than_a_report(self) -> None:
        """``py="xs"`` was passed at some call sites and forgotten at others,
        so two alerts saying equally little were different heights. It comes
        with having no title now, which is the rule behind the prop."""
        assert "py" in _rendered(message("x", "warning"))
        assert "py" not in _rendered(message("x", "failure", title="It broke"))

    def test_a_message_can_carry_a_component(self) -> None:
        """The remote-content bar is a question with two buttons in it."""
        drawn = _rendered(message(soft_button("Allow once"), "warning"))

        assert "ma-btn-soft" in drawn

    def test_the_glyph_moves_but_the_tone_keeps_its_colour(self) -> None:
        drawn = _rendered(message("x", "warning", icon="shield"))

        assert "LucideShield" in drawn
        assert 'color:\\"yellow\\"' in drawn

    def test_a_colour_the_browser_decides_still_comes_through_here(self) -> None:
        """Two call sites compute their own; they keep the padding and the
        glyph size anyway."""
        drawn = _rendered(toned_message("x", rx.cond(True, "red", "green")))

        assert "LucideTriangleAlert" in drawn
        assert "light" in drawn


class TestToasts:
    """The tones again, for news that goes away instead of staying put."""

    def test_each_tone_reaches_sonner_as_its_own_level(self) -> None:
        """The level is the colour and the glyph. A finished run that dropped
        mail and arrived green would read as a success to everybody."""
        seen = {tone: _script(toast("x", tone)) for tone in TONES}

        assert '["error"]' in seen["failure"]
        assert '["warning"]' in seen["warning"]
        assert '["success"]' in seen["success"]
        assert '["info"]' in seen["note"]

    def test_every_tone_the_alerts_have_can_also_be_a_toast(self) -> None:
        """One vocabulary in two lifetimes. A tone added to ``message`` and not
        here fails at the call site with a ``KeyError`` and nowhere else."""
        assert set(LEVELS) == set(get_args(MessageTone))

    def test_a_toast_says_where_it_goes_and_how_long_it_stays(self) -> None:
        """Both are the module's to decide, not the call site's: two toasts
        about one press arriving in different corners are two notifications."""
        drawn = _script(toast("Finished: synced 12 messages.", "success"))

        assert f'"{POSITION}"' in drawn
        assert str(DURATION_MS) in drawn
        assert "Finished: synced 12 messages." in drawn

    def test_a_call_site_can_still_override_one(self) -> None:
        """``setdefault`` and not an assignment — a long report somebody has to
        read is the case that will want it."""
        assert "20000" in _script(toast("x", "note", duration=20000))


class TestBadges:
    def test_a_status_keeps_the_case_the_archive_chose(self) -> None:
        """Mantine uppercases a badge by default, which is how the same status
        read ``idle`` on one page and ``IDLE`` on another."""
        drawn = _rendered(status_badge("idle", "gray"))

        assert 'tt:\\"none\\"' in drawn
        assert "light" in drawn

    def test_a_presence_is_a_dot_rather_than_a_pill(self) -> None:
        assert "dot" in _rendered(dot_badge("KNN ready", "teal"))


class TestEmptyPanel:
    def test_it_is_a_glyph_over_a_sentence(self) -> None:
        drawn = _rendered(empty_panel("inbox", "No imports yet", "Start one."))

        assert "LucideInbox" in drawn
        assert "No imports yet" in drawn
        assert "Start one." in drawn

    def test_an_action_hangs_under_the_sentence(self) -> None:
        drawn = _rendered(
            empty_panel("sparkles", "Nothing yet", "Run it.", primary_button("Rebuild"))
        )

        assert "ma-btn-primary" in drawn

    def test_a_panel_with_no_action_draws_no_action_slot(self) -> None:
        assert "Actions" not in _rendered(empty_panel("inbox", "None", "yet"))


class TestProgress:
    """The row three panels drew separately, and had already drifted."""

    def test_a_job_row_keeps_its_digits_from_jittering(self) -> None:
        """The insights copy had lost ``.ma-tabular`` from both numbers."""
        drawn = _rendered(job_progress(50, "50%", "12 of 24", True))

        assert drawn.count("ma-tabular") == 2

    def test_a_job_row_says_where_the_job_stands(self) -> None:
        """The imports copy had no badge, on the one panel that starts a job."""
        drawn = _rendered(job_progress(50, "50%", "12 of 24", True, status="failed"))

        assert "failed" in drawn

    def test_a_row_with_no_status_draws_no_badge(self) -> None:
        assert "Badge" not in _rendered(job_progress(50, "50%", "12", True))

    def test_a_measurement_is_not_a_job(self) -> None:
        """Never animated: a standing figure should not read as work running."""
        drawn = _rendered(meter_bar(40, "blue"))

        assert "ma-meter" in drawn
        assert "animated:false" in drawn

    def test_a_score_fits_inside_a_table_cell(self) -> None:
        assert "w:64" in _rendered(score_bar(90))


class TestTheRangeSwitch:
    """Chrome, not a field — and in the kit anyway."""

    def test_it_wears_its_own_recipe_and_not_the_field_one(self) -> None:
        drawn = _rendered(range_switch(data=["a", "b"]))

        assert "ma-range" in drawn
        assert "ma-field" not in drawn

    def test_it_has_no_label_and_no_error(self) -> None:
        """It changes what a panel shows and saves nothing, so there is
        nothing for a person to get wrong about it."""
        drawn = _rendered(range_switch(data=["a", "b"]))

        assert "ma-field-label" not in drawn
        assert "error" not in drawn


class TestTheRangeSelect:
    """The switch with more than three positions — chrome, in the kit."""

    def test_it_wears_its_own_recipe_and_not_the_field_one(self) -> None:
        drawn = _rendered(range_select(data=["a", "b"]))

        assert "ma-range-select" in drawn
        assert "ma-field" not in drawn
        assert "ma-field-label" not in drawn

    def test_it_always_holds_a_value(self) -> None:
        """A list has to be grouped some way, and "None" is one of the ways."""
        assert "allowDeselect:false" in _rendered(range_select(data=["a", "b"]))


class TestWhileAReadIsOut:
    """Two placeholders, and the difference is what the reader is owed."""

    def test_a_spinner_is_centred_at_one_padding(self) -> None:
        """It was five copies with ``py`` between ``lg`` and ``xl``, so the
        same wait was a different height depending on which panel waited."""
        drawn = _rendered(spinner())

        assert "Loader" in drawn
        assert "center" in drawn
        assert 'py:\\"lg\\"' in drawn

    def test_a_block_keeps_the_page_from_jumping(self) -> None:
        """Where the size *is* known, a spinner would collapse the card and
        push everything below it up."""
        drawn = _rendered(placeholder_block())

        assert "Skeleton" in drawn
        assert "h:96" in drawn


class TestGraphCanvas:
    """The one wrapped React component in the archive, and its prop contract.

    Nothing here renders cytoscape — the JSX is the browser's business. What a
    test can hold is the contract between the two halves: the box the canvas is
    sized by, the local asset the component is loaded from, and the names the
    props arrive under. Reflex camel-cases every prop on its way out, so
    ``fit_token`` reaches the JSX as ``fitToken``; a wrapper written against the
    Python spelling would silently receive nothing.
    """

    def _canvas(self, **props: Any) -> Any:
        """The wrapped component inside the sized box the builder returns."""
        return graph_canvas(**props).children[0]

    def test_the_builder_is_a_sized_box_wearing_its_class(self) -> None:
        """Cytoscape measures its container, so a box with no height draws a
        canvas nought pixels tall and reports nothing wrong."""
        box = graph_canvas()

        assert "ma-graph-canvas" in _class_of(box)
        assert "height" in json.dumps(box.render()["props"], default=str)

    def test_it_draws_the_wrapped_component(self) -> None:
        assert "GraphCanvas" in _rendered(graph_canvas())

    def test_it_is_loaded_from_the_local_jsx_asset(self) -> None:
        """A shared asset, so the file ships with the package rather than with
        whichever application happens to import it."""
        library = self._canvas().library

        assert library.startswith("$/public/")
        assert library.endswith("graph_canvas.jsx")

    def test_it_declares_the_lazy_loading_imports(self) -> None:
        """A ``NoSSRComponent`` is compiled into a dynamic import written
        against ``ClientSide``; without these two the page references a name
        nothing defines, and only the browser says so."""
        found = self._canvas().add_imports()

        assert "lazy" in found["react"]
        assert any(name.endswith("/context") for name in found)

    def test_cytoscape_arrives_as_a_pinned_dependency(self) -> None:
        assert "cytoscape@3.34.2" in self._canvas().lib_dependencies

    def test_every_prop_reaches_the_jsx_camel_cased(self) -> None:
        drawn = _rendered(
            graph_canvas(
                elements=[{"group": "nodes", "data": {"id": "a"}}],
                stylesheet=[{"selector": "node", "style": {"label": "data(label)"}}],
                layout={"name": "cose"},
                selected="a",
                fit_token=3,
            )
        )

        assert "fitToken:3" in drawn
        assert "elements" in drawn
        assert "stylesheet" in drawn
        assert "layout" in drawn
        assert "selected" in drawn

    def test_the_three_callbacks_are_event_triggers(self) -> None:
        triggers = self._canvas().get_event_triggers()

        assert {"on_select", "on_expand", "on_background"} <= set(triggers)

    def test_a_tap_and_a_double_tap_both_carry_the_node_id(self) -> None:
        drawn = _rendered(
            graph_canvas(
                on_select=rx.console_log("picked"),
                on_expand=rx.console_log("expanded"),
            )
        )

        assert "onSelect" in drawn
        assert "onExpand" in drawn

    def test_a_background_tap_carries_nothing_and_still_builds(self) -> None:
        """The guard on a simplification that looks free and is not.

        ``on_background`` takes no argument, and the obvious spellings of that
        are ``EventHandler[lambda: []]`` — which ``ruff``'s PIE807 rewrites —
        and ``EventHandler[list]``, which is what it rewrites it *to*. The
        second one is broken: Reflex reads the spec's signature to name the
        event's arguments, ``list`` has an unannotated ``iterable``, and the
        component raises ``MissingAnnotationError`` the moment a page hands it
        a handler. Every other test in this file passes with the rewrite in.
        """
        assert "onBackground" in _rendered(
            graph_canvas(on_background=rx.console_log("cleared"))
        )


class TestTheDocumentViewers:
    """The three wrapped Office renderers, and their contract with the JSX.

    Nothing here renders a document — that is the browser's business, and the
    browser check is where a rendered slide is confirmed. What a test can hold
    is everything the two halves have to agree on and nothing enforces: the
    box each is sized by, the local asset each is loaded from, the exact npm
    version each is pinned to, and the names the props arrive under.

    The pins are asserted one by one rather than as a set. A renderer's
    defaults *are* the design, so a range that silently took a minor release
    would re-lay every document in the archive, and the test that would notice
    is this one.
    """

    VIEWERS = {
        "docx": (docx_viewer, "DocxViewer", "@extend-ai/react-docx@0.9.1"),
        "xlsx": (xlsx_viewer, "XlsxViewer_", "@extend-ai/react-xlsx@0.16.3"),
        "pptx": (pptx_viewer, "PptxViewer", "@extend-ai/react-pptx@0.2.0"),
        "pdf": (pdf_viewer, "PdfViewer", "@embedpdf/pdfium@2.15.0"),
    }

    def _viewer(self, name: str, **props: Any) -> Any:
        """The wrapped component inside the sized box the builder returns."""
        return self.VIEWERS[name][0](**props).children[0]

    @pytest.mark.parametrize("name", sorted(VIEWERS))
    def test_the_builder_is_a_sized_box_wearing_its_class(self, name: str) -> None:
        """Each renderer measures its container once, at mount. A box with no
        height draws a document nought pixels tall and reports nothing wrong —
        the same trap ``graph_canvas`` documents."""
        box = self.VIEWERS[name][0]()

        assert "ma-viewer" in _class_of(box)
        assert "height" in json.dumps(box.render()["props"], default=str)

    @pytest.mark.parametrize("name", sorted(VIEWERS))
    def test_it_draws_the_wrapped_component(self, name: str) -> None:
        tag = self.VIEWERS[name][1]

        assert tag in _rendered(self.VIEWERS[name][0]())

    @pytest.mark.parametrize("name", sorted(VIEWERS))
    def test_it_is_loaded_from_the_local_jsx_asset(self, name: str) -> None:
        """All three from one folder, which is what lets them share
        ``viewer_shell.jsx`` and ``viewer_bytes.js``. ``rx.asset`` keys the
        folder on the *calling* module, so registering them anywhere but
        ``viewer_assets.py`` would scatter them and break those imports."""
        library = self._viewer(name).library

        assert library.startswith("$/public/")
        assert library.endswith(f"viewer_assets/viewer_{name}.jsx")

    @pytest.mark.parametrize("name", sorted(VIEWERS))
    def test_it_declares_the_lazy_loading_imports(self, name: str) -> None:
        """A ``NoSSRComponent`` is compiled into a dynamic import written
        against ``ClientSide``; without these two the page references a name
        nothing defines, and only the browser says so."""
        found = self._viewer(name).add_imports()

        assert "lazy" in found["react"]
        assert any(one.endswith("/context") for one in found)

    @pytest.mark.parametrize("name", sorted(VIEWERS))
    def test_the_renderer_arrives_as_a_pinned_dependency(self, name: str) -> None:
        assert self.VIEWERS[name][2] in self._viewer(name).lib_dependencies

    @pytest.mark.parametrize("name", sorted(VIEWERS))
    def test_every_prop_reaches_the_jsx_camel_cased(self, name: str) -> None:
        """``file_name`` reaches the JSX as ``fileName``. A wrapper written
        against the Python spelling receives ``undefined`` and says nothing."""
        drawn = _rendered(
            self.VIEWERS[name][0](encoded="UEsDBA==", file_name="Bericht.docx")
        )

        assert "fileName:" in drawn
        assert "encoded:" in drawn
        assert "file_name" not in drawn

    def test_the_pdf_registers_every_plugin_its_header_offers(self) -> None:
        """embedpdf is headless — a capability the header shows and no plugin
        provides is a dead button, and one registered for nothing is weight in
        a desktop bundle. The header offers page navigation, zoom, rotation
        and a rail, so those four plugins and their three foundations are the
        whole list."""
        found = set(self._viewer("pdf").lib_dependencies)

        assert found == {
            "@embedpdf/core@2.15.0",
            "@embedpdf/engines@2.15.0",
            "@embedpdf/models@2.15.0",
            "@embedpdf/pdfium@2.15.0",
            "@embedpdf/plugin-document-manager@2.15.0",
            "@embedpdf/plugin-interaction-manager@2.15.0",
            "@embedpdf/plugin-render@2.15.0",
            "@embedpdf/plugin-rotate@2.15.0",
            "@embedpdf/plugin-scroll@2.15.0",
            "@embedpdf/plugin-thumbnail@2.15.0",
            "@embedpdf/plugin-viewport@2.15.0",
            "@embedpdf/plugin-zoom@2.15.0",
        }

    def test_only_the_workbook_is_told_which_way_the_theme_goes(self) -> None:
        """A spreadsheet paints its own grid and its own cell fills, so left
        to a default it is a white sheet in a dark panel. A document and a
        deck draw paper, which is white in either theme."""
        assert "isDark:" in _rendered(xlsx_viewer(is_dark=True))
        assert not hasattr(self._viewer("docx"), "is_dark")
        assert not hasattr(self._viewer("pptx"), "is_dark")

    def test_the_pdf_overrides_both_of_its_cdn_defaults(self) -> None:
        """The two lines that keep the offline bundle working.

        ``usePdfiumEngine`` defaults to fetching its WebAssembly from
        ``cdn.jsdelivr.net`` and its fallback typefaces from the same place.
        That default constant is still compiled into the bundle — it is the
        library's, and unreachable — so the absence of a URL in *our* source
        is not on its own proof. What is proof is that both defaults are
        overridden at the call site, which is what this asserts.

        Removing either is invisible on a developer machine with internet and
        renders blank pages in the desktop bundle.
        """
        source = (Path(viewer_assets.__file__).parent / "viewer_pdf.jsx").read_text(
            encoding="utf-8"
        )

        assert "wasmUrl: pdfiumWasmUrl" in source
        assert "fontFallback: null" in source
        assert 'from "@embedpdf/pdfium/pdfium.wasm?url"' in source

    @pytest.mark.parametrize("name", sorted(VIEWERS))
    def test_every_viewer_is_exported_through_the_error_boundary(
        self, name: str
    ) -> None:
        """A parser that throws while rendering must not take the panel down.

        Each of these hands a vendored WebAssembly parser a file a stranger
        wrote, and a malformed ``.docx`` or ``.pptx`` — routine in a real
        archive — makes several of them throw *synchronously during render*.
        React answers a throw in render by unmounting the tree above it, so an
        unwrapped viewer breaks ``preview.py``'s promise that every file gets a
        panel, and breaks it for exactly the files that most need one.

        Read as text and asserted per file because that is the only place the
        fact lives: the boundary is a React class the Python wrapper cannot see.
        ``viewer_pdf.jsx`` was the one export that went without it, for as long
        as each viewer wrote the same four-line wrapper by hand — which is why
        the wrapping is a helper now and why this walks all four.
        """
        source = (Path(viewer_assets.__file__).parent / f"viewer_{name}.jsx").read_text(
            encoding="utf-8"
        )

        shell = (Path(viewer_assets.__file__).parent / "viewer_shell.jsx").read_text(
            encoding="utf-8"
        )

        assert "export function withBoundary" in shell, "the helper is not exported"
        assert "withBoundary" in source, f"viewer_{name}.jsx imports no boundary"
        assert "export default withBoundary(" in source, (
            f"viewer_{name}.jsx exports its component unwrapped"
        )

    def test_the_tables_and_the_wrappers_name_the_same_viewers(self) -> None:
        """One key per viewer, across both halves of the pair.

        ``viewer_assets.py`` holds the npm pins and the JSX paths and
        ``viewer.py`` holds the wrapper classes, and a fifth viewer added to
        one and not the other is a library installed for a component that does
        not exist, or the reverse.
        """
        assert set(VIEWER_PACKAGES) == set(VIEWER_LIBRARIES)
        assert set(VIEWER_PACKAGES) == set(self.VIEWERS)

    def test_no_viewer_reaches_for_a_cdn(self) -> None:
        """The archive's posture is that the browser never fetches anything.

        Every renderer here resolves WebAssembly, and one of the families this
        feature draws on defaults to pulling it from a public CDN — which
        would be invisible on a developer machine with internet and fatal in
        the offline desktop bundle. So the JSX is read as text and any
        absolute URL in it fails, which is a rule a dependency bump cannot
        quietly undo.
        """
        folder = Path(viewer_assets.__file__).parent
        offenders = {
            path.name: [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if "http://" in line or "https://" in line
            ]
            for path in sorted(folder.glob("viewer_*.js*"))
        }

        assert {name: found for name, found in offenders.items() if found} == {}
