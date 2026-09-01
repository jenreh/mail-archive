"""A form says what is wrong under the box that is wrong.

Three things, and they are separable on purpose.

The mixin is the shape — a map of field name to message, and the three
questions a form asks of it. The rules are not here: what makes a mailbox
address valid belongs to the state that owns the address, and the two forms
that validate are checked against their own rules in their own test files.

The field is the other half. Every one of these controls already knows how to
be invalid — a red border, ``aria-invalid`` on the input, the message printed
under it — so what is asserted here is that a kit field passes ``error``
through to that mechanism rather than a form drawing its own red text. It is
the whole reason the complaint lives in state at all: a page-level alert would
put the sentence somewhere other than the box a person has to fix.

And the two together: a state var reaches the field, so the message a rule
writes is the one that appears under the control.
"""

import json
from typing import Any

import pytest
import reflex as rx

from mailarc_ui.kit import REQUIRED, FieldErrors, input_field, select_field


class Form(FieldErrors, rx.State):
    """A form with the mixin on it, and nothing else — the shape under test."""

    name: str = ""


@pytest.fixture
def form() -> Form:
    """Reflex refuses to construct a state outside a test process."""
    return Form()


def _rendered(component: Any) -> str:
    """One component's whole render tree, as something searchable."""
    return json.dumps(component.render(), default=str)


class TestTheMap:
    def test_a_fresh_form_complains_about_nothing(self, form: Form) -> None:
        assert form.errors == {}
        assert form.has_errors is False

    def test_a_failure_lands_under_its_own_field(self, form: Form) -> None:
        form._fail("email", "Required.")

        assert form.errors == {"email": "Required."}
        assert form.has_errors is True

    def test_a_field_that_was_fixed_stops_complaining(self, form: Form) -> None:
        form._fail("email", "Required.")

        form._pass("email")

        assert form.errors == {}
        assert form.has_errors is False

    def test_clearing_a_field_nobody_complained_about_is_harmless(
        self, form: Form
    ) -> None:
        form._pass("email")

        assert form.errors == {}

    def test_one_bad_field_out_of_several_is_still_a_bad_form(self, form: Form) -> None:
        form._fail("email", "Required.")
        form._pass("name")

        assert form.has_errors is True

    def test_check_records_a_message_and_takes_it_back(self, form: Form) -> None:
        """The shape every rule ends in, so that a rule is written as "what is
        wrong with this value, or nothing" rather than as two branches."""
        assert form._check("email", "Required.") is False
        assert form.errors["email"] == "Required."

        assert form._check("email", "") is True
        assert form.errors == {}

    def test_required_is_the_rule_both_forms_share(self, form: Form) -> None:
        assert form._required("name", "   ") is False
        assert form.errors["name"] == REQUIRED

        assert form._required("name", " Work ") is True
        assert form.errors == {}

    def test_starting_over_forgets_every_complaint(self, form: Form) -> None:
        """A different mailbox opened. Without this the complaint about the one
        somebody was looking at a moment ago stays on screen against the values
        of the one they opened next."""
        form._fail("email", "Required.")
        form._fail("host", "Required.")

        form._clear_errors()

        assert form.errors == {}


class TestAFieldSaysIt:
    def test_a_field_hands_its_error_to_the_control(self) -> None:
        """Mantine's own mechanism: the control goes red, sets
        ``aria-invalid`` and prints the message under itself."""
        drawn = _rendered(input_field(label="Email", error="Required."))

        assert "error" in drawn
        assert "Required." in drawn

    def test_a_field_with_nothing_wrong_says_nothing(self) -> None:
        assert "error" not in _rendered(input_field(label="Email"))

    def test_the_message_reaches_the_field_from_the_state(self) -> None:
        """The two halves joined: a rule writes into the map, and the field
        indexed by that name is what draws it."""
        drawn = _rendered(input_field(label="Email", error=Form.errors["email"]))

        assert "errors" in drawn
        assert "email" in drawn

    def test_every_kind_of_field_carries_one(self) -> None:
        """It rides on ``**props`` rather than being declared per function, so
        this is what says a new field type will not quietly drop it."""
        for field in (input_field, select_field):
            assert "error" in _rendered(field(label="x", error="Required."))
