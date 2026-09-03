"""One form recipe in the application, and one place it is written.

The search form was built out of ``kit.inputs`` from the day it was drawn: a
mono-uppercase label, an optional right-aligned hint, and under it a filled
control wearing ``.ma-field``. The two forms written before it — the mailbox
form and the embedder settings — were plain ``mn.text_input(label=…)``, which
draws Mantine's own sentence-case label in the default variant. Two forms in
one application, disagreeing about what a field looks like, and neither one
wrong on its own.

Two kinds of check, for the two ways that comes back.

The first reads the source, like ``test_ui_kit_is_the_only_card``: a module
that reaches for ``mn.text_input`` again gets a form that looks reasonable in
its own diff and wrong beside every other page. ``ast`` and not a ``grep``,
because a docstring naming the call is not the call.

The second reads the render, because the source check cannot see a *half*
conversion. A kit field whose label went back into the control would draw two
labels — the archive's over the box and Mantine's inside it — so every form
here is asserted to carry exactly as many mono labels as it has controls, and
no Mantine ``label`` or ``description`` prop at all.
"""

import ast
import json
from pathlib import Path
from typing import Any

import mailarc_ui
from mailarc_ui.accounts.components import account_settings, add_account_form
from mailarc_ui.embedder.components import api_key_field, settings_form
from mailarc_ui.graph.state import GraphExplorerState
from mailarc_ui.search.form import search_form
from mailarc_ui.tags.components import promote_form


def _promote_form() -> Any:
    """The tag form, bound to the state that hosts the mixin on ``/graph``.

    A closure because the form takes the cluster it is about — the two hosts of
    ``TagActionsState`` hold that differently — and because every entry in
    :data:`FORMS` is called with no arguments.
    """
    return promote_form(GraphExplorerState, "topic", "topic:example")


VALIDATING = {
    "accounts (add a mailbox)": (add_account_form, 4),
    "accounts (the open mailbox)": (account_settings, 3),
    "embedder (settings)": (settings_form, 2),
}
"""The forms that validate, and how many controls on each must be bound.

The counts are what makes this a real check rather than a smoke test, and
each was arrived at by counting the boxes on screen. Add: provider, address,
and the credential field in both branches of its ``secret`` cond. The open
mailbox: address, and the credential field's two branches. The embedder:
dimension and base URL — the model box and the provider select have no rule,
and the key box cannot have one, since an empty key means "keep the stored
one".

A number rather than "at least one" because the bug this was written for was
exactly a partial binding: the address carried its error and the generated
credential field did not, so the form marked one empty required box and stayed
silent about the other. Every state test passed — they call the handler, which
fills the map correctly; nothing had ever asserted the map reached the boxes.
"""

PACKAGE = Path(mailarc_ui.__file__).parent
"""The installed sources, found through the import — see
``test_ui_kit_is_the_only_card`` for why not a relative path."""

FIELD_HOME = PACKAGE / "kit" / "inputs.py"
"""The one module allowed to build a form control."""

CONTROLS = frozenset(
    {
        # text and number
        "text_input",
        "textarea",
        "password_input",
        "number_input",
        "json_input",
        "masked_input",
        "pin_input",
        # selection
        "select",
        "multi_select",
        "native_select",
        "rich_select",
        "tree_select",
        "autocomplete",
        "tags_input",
        "pills_input",
        # toggles
        "checkbox",
        "radio",
        "switch",
        "chip",
        "segmented_control",
        "rating",
        # date and time
        "date_input",
        "date_picker",
        "date_picker_input",
        "date_time_picker",
        "inline_date_time_picker",
        "month_picker",
        "month_picker_input",
        "year_picker",
        "year_picker_input",
        "time_input",
        "time_picker",
        # everything else that collects a value
        "slider",
        "range_slider",
        "file_input",
        "color_input",
        "color_picker",
        "angle_slider",
        "hue_slider",
        "alpha_slider",
    }
)
"""Every ``mn.*`` that collects a value from a person.

Deliberately the whole vocabulary rather than the six this application happens
to use today, because that is the difference between a guard and a record of
the past: the next form to be written will reach for whichever control it
needs, and the one that is not on this list is the one that quietly gets
Mantine's own label back. A control with no kit field yet is not an exception
to the rule — it is a missing ``kit.inputs`` function, and this failing is
where that gets noticed.

``dropzone`` and ``rich_text_editor`` are left out: both are composite
surfaces with chrome of their own rather than a label over a box, and neither
would be drawn by wrapping it in :func:`~mailarc_ui.kit.input_field`.
"""

FIELD_CLASS = 'className:\\"ma-field\\"'
"""How a kit field's hook appears in a rendered prop list."""

VAR_KEYED = 'errors_rx_state_?.[field_rx_state_?.[\\"name\\"'
"""``errors[field.name]`` as the render spells it — a lookup whose key is a
state path rather than a literal."""

MANTINE_OWN = ("label:", "description:")
"""The two props that would draw a second label and a second caption."""

FORMS = {
    "search": search_form,
    "accounts (add a mailbox)": add_account_form,
    "accounts (the open mailbox)": account_settings,
    "embedder (settings)": settings_form,
    "embedder (the key)": api_key_field,
    "tags (promote a cluster)": _promote_form,
}
"""Every form this application draws, by the name a failure should say."""


def _control_of(func: ast.expr) -> str | None:
    """The control a call's callee names, if it names one at all.

    Two shapes, because several of these are namespaces: ``mn.checkbox(…)``
    is one control and ``mn.checkbox.group(…)`` is the same control's plural.
    Matching only the first shape would have let a whole family of fields —
    ``checkbox.group``, ``radio.group``, ``chip.group`` — through the guard.
    """
    if not isinstance(func, ast.Attribute):
        return None
    if isinstance(func.value, ast.Name) and func.value.id == "mn":
        return func.attr if func.attr in CONTROLS else None
    inner = func.value
    if (
        isinstance(inner, ast.Attribute)
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "mn"
        and inner.attr in CONTROLS
    ):
        return inner.attr
    return None


def _mantine_controls(source: Path) -> set[str]:
    """Which ``mn.<control>(…)`` calls this module actually makes."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (name := _control_of(node.func)):
            found.add(name)
    return found


def _drawn(component: Any) -> str:
    """One component's whole render tree, as something searchable."""
    return json.dumps(component.render(), default=str)


class TestOnlyTheKitBuildsAField:
    def test_no_module_reaches_past_kit_inputs_for_a_control(self) -> None:
        offenders = {
            str(path.relative_to(PACKAGE)): sorted(used)
            for path in PACKAGE.rglob("*.py")
            if path != FIELD_HOME and (used := _mantine_controls(path))
        }

        assert offenders == {}, (
            "these modules build their own form control instead of using a "
            f"kit field: {offenders}"
        )

    def test_the_dashboard_switch_went_to_the_kit_too(self) -> None:
        """The last exemption, and it stopped needing to be one.

        The switch above the two charts is chrome rather than a field — no
        label, no value anybody saves — so it kept its own recipe in
        ``.ma-range``. That made it the single module allowed to call
        ``mn.segmented_control``, which is a rule nobody reading
        ``dashboard/components.py`` would have known. It is
        ``kit.range_switch`` now and the exemption is gone, which is the only
        state this guard can be trusted in: one home, no exceptions to
        remember.
        """
        assert "ma-range" in (FIELD_HOME).read_text(encoding="utf-8")


class TestEveryFormIsDrawnAlike:
    def test_every_control_wears_the_class_the_stylesheet_keys_on(self) -> None:
        bare = {
            name: drawn.count(FIELD_CLASS)
            for name, form in FORMS.items()
            if (drawn := _drawn(form())).count(FIELD_CLASS) == 0
        }

        assert bare == {}, f"these forms draw no kit field at all: {bare}"

    def test_every_control_carries_a_mono_uppercase_label(self) -> None:
        """Counted against the controls rather than merely present: a form
        that converted three fields and left the fourth would still pass a
        check for "is there a label on this page"."""
        mismatched = {
            name: (drawn.count(FIELD_CLASS), drawn.count("ma-field-label"))
            for name, form in FORMS.items()
            if (drawn := _drawn(form())).count(FIELD_CLASS)
            != drawn.count("ma-field-label")
        }

        assert mismatched == {}, (
            f"controls and mono labels disagree (fields, labels): {mismatched}"
        )

    def test_no_form_lets_mantine_draw_a_second_label_or_caption(self) -> None:
        """The two props that would put a sentence-case label inside the box
        and a second caption above it, beside the ones the kit draws."""
        trees = {name: _drawn(form()) for name, form in FORMS.items()}
        offenders = {
            name: [prop for prop in MANTINE_OWN if prop in tree]
            for name, tree in trees.items()
            if any(prop in tree for prop in MANTINE_OWN)
        }

        assert offenders == {}, f"Mantine draws a second label here: {offenders}"


class TestEveryValidatedFieldIsWiredToTheMap:
    """A rule that fills the map is worth nothing if no box reads it.

    Read off the render and counted, because the two halves are written in
    different files and only meet in the browser: ``accounts/state.py`` writes
    ``errors[name]`` and ``accounts/components.py`` has to pass that same
    ``errors[name]`` to the control. Nothing in either file is wrong on its
    own when the second half is missing.
    """

    def test_each_form_binds_as_many_controls_as_it_has_rules(self) -> None:
        short = {
            name: (_drawn(form()).count("errors"), expected)
            for name, (form, expected) in VALIDATING.items()
            if _drawn(form()).count("errors") != expected
        }

        assert short == {}, f"(bound, expected) per form: {short}"

    def test_a_generated_field_is_bound_by_the_name_its_provider_gave_it(
        self,
    ) -> None:
        """The one a literal key cannot cover, and the one that was missing.

        A provider declares the field at runtime, so the box indexes the map by
        a ``Var`` — ``errors[field.name]`` — which compiles to a lookup whose
        *key* is itself a state path. The literal-key bindings beside it were
        there all along; this one was silently dropped, and the form marked one
        empty required box while staying silent about the other.
        """
        drawn = _drawn(add_account_form())

        assert VAR_KEYED in drawn

    def test_the_search_form_binds_nothing(self) -> None:
        """It has no rules: every field narrows a question, and a question
        narrowed to nothing is an empty result rather than a mistake."""
        assert "errors" not in _drawn(search_form())
