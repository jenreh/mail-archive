"""The state half of :mod:`mailarc_ui.kit.inputs` — what fills a field's ``error``.

A form in this archive holds one map of field name to message and nothing else.
Not a validation framework: the rules themselves stay in the state that owns
the values, because what makes a mailbox address valid is that state's
business and not this module's. What lives here is the shape both forms keep
it in, and the three questions every form asks of it.

**A map rather than a var per field**, which is the one design decision worth
arguing. The appkit dialogs this is modelled on declare ``model_id_error``,
``text_error``, ``processor_type_error`` — a var each, which reads well until a
form's fields are not known when it is written. The accounts form's are not:
a provider declares them, so an IMAP mailbox draws four boxes and a Gmail one
draws none, and there is no var to declare for a field whose name arrives at
runtime. One map answers both — ``errors["email_address"]`` for a field this
repository spelled out, ``errors[field.name]`` for one a provider did.

A missing key is not an error. It reaches the browser as ``undefined``, which
is exactly what Mantine reads as "this field is fine", so a form starts clean
without having to enumerate what it is not complaining about yet.

**Validation runs on the setter, not only on submit.** A message that appears
when a person types and leaves when they fix it is the whole reason to hold
this in state rather than raising at save time; a form that stays silent until
Save and then reports four things at once is a form people submit twice.
"""

from __future__ import annotations

import reflex as rx

REQUIRED = "Required."
"""What an empty field that must be filled says.

One sentence rather than one per form: a person who meets it twice should not
have to work out whether the two mean the same thing.
"""


class FieldErrors(rx.State, mixin=True):
    """One form's complaints, keyed by field name.

    Mixed into a state rather than owned by one, because both forms that
    validate need it and the accounts form needs it for fields that do not
    exist until a provider is chosen.
    """

    errors: dict[str, str] = {}
    """Field name to the message under that field. Absent means valid."""

    @rx.var
    def has_errors(self) -> bool:
        """Whether anything on this form is complaining.

        What a submit button's ``disabled`` reads, so the rule for "can this be
        saved" lives with the values rather than in the component drawing the
        button.
        """
        return any(self.errors.values())

    def _fail(self, name: str, message: str) -> None:
        """Record what is wrong with one field."""
        self.errors = {**self.errors, name: message}

    def _pass(self, name: str) -> None:
        """Take back a complaint about one field."""
        if name in self.errors:
            self.errors = {
                key: value for key, value in self.errors.items() if key != name
            }

    def _check(self, name: str, message: str) -> bool:
        """Record ``message`` where it is truthy, clear the field where it is not.

        The shape every rule ends in, so that a rule is written as "what is
        wrong with this value, or nothing" and never as two branches that can
        disagree about which one clears the field.
        """
        if message:
            self._fail(name, message)
            return False
        self._pass(name)
        return True

    def _required(self, name: str, value: str) -> bool:
        """The one rule every form here shares."""
        return self._check(name, "" if value.strip() else REQUIRED)

    def _clear_errors(self) -> None:
        """Start over — a different mailbox opened, or the form was reset.

        Its own method because forgetting it is a specific and confusing bug:
        the complaint about the mailbox somebody was looking at a moment ago
        stays on screen against the values of the one they opened next.
        """
        self.errors = {}
