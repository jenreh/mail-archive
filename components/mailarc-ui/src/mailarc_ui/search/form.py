"""The question, as a form: the left column of the search page.

Layout and nothing else — every value comes from :class:`MailSearchState` and
nothing here opens a graph. Every control is a ``kit.inputs`` field, so the
mono-uppercase label, the translucent fill, the hairline and the coral focus
ring are stated once in ``assets/css/mail-archive.css`` and not re-passed at
any call site.

The form has two states rather than one, and that is the whole design of it.
On the full-text path every field narrows the answer. On the semantic path the
KNN can honour the question and nothing else, so the structured half goes
**disabled** and a sentence beside the selector says why — a form that quietly
stops honouring what is typed in it is the one way a search can lie about what
it searched.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.kit import (
    date_field,
    input_field,
    primary_button,
    quiet_button,
    segmented_field,
    select_field,
)
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    SEMANTIC_IS_TEXT_ONLY,
)
from mailarc_ui.search.state import MailSearchState

FORM_WIDTH = 300
"""The left column, in pixels. Wide enough for a two-up date row."""

FIELD_GAP = 18
"""What the design puts between one field and the next."""

PAIR_GAP = 12
"""And between the two halves of a two-up row."""

LABEL_GAP = 8
"""A note sits as close under its control as a control does under its label."""

ATTACHMENT_SEGMENTS = [
    {"label": "Any", "value": ATTACH_ANY},
    {"label": "With", "value": ATTACH_WITH},
    {"label": "Without", "value": ATTACH_WITHOUT},
]
"""Three positions, because "has attachments" is a tri-state — see
:data:`~mailarc_ui.search.model.ATTACH_ANY`."""


def _question() -> rx.Component:
    """The one field both paths read."""
    return input_field(
        label="Search",
        placeholder="Words in the message",
        value=MailSearchState.query,
        on_change=MailSearchState.set_query,
        on_key_down=MailSearchState.search_on_enter,
        left_section=rx.icon("search", size=15),
    )


def _mode() -> rx.Component:
    """Which path answers, and what is off about the one that cannot.

    ``data`` is a computed var rather than a constant: only the state knows
    whether an embedder exists, and the semantic segment is ``disabled``
    until one does.
    """
    return mn.stack(
        segmented_field(
            label="Mode",
            data=MailSearchState.mode_options,
            value=MailSearchState.mode,
            on_change=MailSearchState.choose_mode,
        ),
        _note(MailSearchState.semantic_note != "", MailSearchState.semantic_note),
        _note(MailSearchState.semantic_chosen, SEMANTIC_IS_TEXT_ONLY),
        gap=LABEL_GAP,
    )


def _people() -> rx.Component:
    """Who it came from and who it went to, matched by containment."""
    return mn.stack(
        input_field(
            label="From",
            hint="contains",
            placeholder="name or address",
            value=MailSearchState.sender,
            on_change=MailSearchState.set_sender,
            disabled=MailSearchState.semantic_chosen,
        ),
        input_field(
            label="To",
            hint="contains",
            placeholder="name or address",
            value=MailSearchState.recipient,
            on_change=MailSearchState.set_recipient,
            disabled=MailSearchState.semantic_chosen,
        ),
        gap=FIELD_GAP,
    )


def _dates() -> rx.Component:
    """The window, as the design draws it: two fields on one row."""
    return mn.simple_grid(
        date_field(
            label="From",
            value=MailSearchState.date_from,
            on_change=MailSearchState.set_date_from,
            disabled=MailSearchState.semantic_chosen,
            value_format="DD.MM.YYYY",
            placeholder="any",
            clearable=True,
        ),
        date_field(
            label="To",
            value=MailSearchState.date_to,
            on_change=MailSearchState.set_date_to,
            disabled=MailSearchState.semantic_chosen,
            value_format="DD.MM.YYYY",
            placeholder="any",
            clearable=True,
        ),
        cols=2,
        spacing=PAIR_GAP,
    )


def _attachments() -> rx.Component:
    return segmented_field(
        label="Attachments",
        data=ATTACHMENT_SEGMENTS,
        value=MailSearchState.attachments,
        on_change=MailSearchState.choose_attachments,
        disabled=MailSearchState.semantic_chosen,
    )


def _account() -> rx.Component:
    """Which mailbox the copy was imported from; empty means all of them."""
    return select_field(
        label="Account",
        data=MailSearchState.accounts,
        value=MailSearchState.account_id,
        on_change=MailSearchState.choose_account,
        disabled=MailSearchState.semantic_chosen,
        placeholder="all accounts",
        clearable=True,
    )


def _note(when: rx.Var | bool, text: rx.Var | str) -> rx.Component:
    """One quiet line under a control, or nothing at all."""
    return rx.cond(when, mn.text(text, size="xs", c="dimmed"), rx.fragment())


def _messages() -> rx.Component:
    """The two things a search can say back, above the buttons.

    Kept apart on purpose. A notice is about the question or the
    configuration and is shown as written; an error is a fault, and red.
    """
    return mn.stack(
        rx.cond(
            MailSearchState.error != "",
            mn.text(MailSearchState.error, size="sm", c="red.7"),
            rx.fragment(),
        ),
        rx.cond(
            MailSearchState.notice != "",
            mn.text(MailSearchState.notice, size="sm", c="dimmed"),
            rx.fragment(),
        ),
        gap=6,
    )


def _actions() -> rx.Component:
    """The one accent button on this page, and the quiet one beside it.

    ``disabled`` is the negation of a computed var rather than a rule spelled
    out here: what counts as a question worth asking is the state's business,
    and a button that can be pressed over a form that cannot answer is a
    promise.
    """
    return mn.group(
        primary_button(
            "Search",
            on_click=MailSearchState.submit,
            loading=MailSearchState.searching,
            disabled=~MailSearchState.can_search,
            flex="1",
        ),
        quiet_button("Reset", on_click=MailSearchState.reset_form),
        gap=LABEL_GAP,
        wrap="nowrap",
        w="100%",
    )


def search_form() -> rx.Component:
    """The whole left column, scrolling on its own."""
    return mn.scroll_area(
        mn.stack(
            _question(),
            _mode(),
            _people(),
            _dates(),
            _attachments(),
            _account(),
            _messages(),
            _actions(),
            gap=FIELD_GAP,
            p="md",
            w="100%",
        ),
        type="hover",
        # ScrollArea takes no Mantine style props; unknown kwargs become CSS
        # keys, so the height has to be spelled out as CSS.
        style={"height": "100%"},
    )
