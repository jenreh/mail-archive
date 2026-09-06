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
    FIELD_GAP,
    LABEL_GAP,
    OPTION_LIMIT,
    date_field,
    field_note,
    input_field,
    multi_select_field,
    primary_button,
    quiet_button,
    range_switch,
    segmented_field,
    select_field,
)
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    MATCH_ALL,
    MATCH_ANY,
    NATURAL_IS_INTERPRETED,
    SEMANTIC_IS_TEXT_ONLY,
)
from mailarc_ui.search.state import MailSearchState

FORM_WIDTH = 300
"""The left column, in pixels. Wide enough for a two-up date row."""

PAIR_GAP = 12
"""What the design puts between the two halves of a two-up row.

The gap *between* fields, and the one under a label, are the kit's
:data:`~mailarc_ui.kit.FIELD_GAP` and :data:`~mailarc_ui.kit.LABEL_GAP` —
every form in the archive is spaced alike, so both live there, not here.
"""

ATTACHMENT_SEGMENTS = [
    {"label": "Any", "value": ATTACH_ANY},
    {"label": "With", "value": ATTACH_WITH},
    {"label": "Without", "value": ATTACH_WITHOUT},
]
"""Three positions, because "has attachments" is a tri-state — see
:data:`~mailarc_ui.search.model.ATTACH_ANY`."""

MATCH_SEGMENTS = [
    {"label": "all", "value": MATCH_ALL},
    {"label": "any", "value": MATCH_ANY},
]
"""What several picked recipients mean — see
:data:`~mailarc_ui.search.model.MATCH_ALL`.

Lower case and unlabelled, because it sits in the **To** label's hint slot
rather than under a label of its own: it is not a value anybody saves, it is
how to read the field beneath it. :func:`~mailarc_ui.kit.range_switch` is the
kit's spelling of exactly that — chrome, not a field.
"""

ANY_SENDER = "any"
"""The **From** label's hint — where **To** carries a switch, this carries a
word.

Said rather than left to be inferred, because the two fields look alike and
read differently: several senders can only mean *any of these*, and a picker
that silently meant something else than the one under it is the kind of thing
nobody notices until a result list is wrong.
"""

NOT_ARCHIVED = "no match in the archive"
"""What a searchable picker says when nothing it holds matches the typing.

Its own sentence rather than Mantine's *No options*, because the two are
different facts: the list is not empty, this archive simply never saw that
address.
"""


def _question() -> rx.Component:
    """The one field both paths read."""
    return input_field(
        label="Search",
        placeholder="Words in the message",
        value=MailSearchState.query,
        on_change=MailSearchState.set_query,
        on_key_down=MailSearchState.search_on_enter,
        on_clear=MailSearchState.clear_query,
        left_section=rx.icon("search", size=15),
    )


def _mode() -> rx.Component:
    """Which path answers, and what is off about the ones that cannot.

    ``data`` is a computed var rather than a constant: only the state knows
    whether an embedder and an interpreter exist, and each of the two optional
    segments is ``disabled`` until its own does.

    Four notes under one control, and they say different kinds of thing. The
    two ``*_note`` vars name a setting that is missing. :data:`SEMANTIC_IS_TEXT_ONLY`
    and :data:`NATURAL_IS_INTERPRETED` describe what the chosen segment does to
    the fields below — one stops honouring them, the other fills them in — and
    they are opposite promises, so neither may be inferred from the other.
    :attr:`~mailarc_ui.search.state.MailSearchState.understood` is the only one
    that is about a *question* rather than about the form, which is why it is a
    var of its own and not the answer's ``notice``: it has to survive **Load
    more**.
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
        _note(MailSearchState.llm_note != "", MailSearchState.llm_note),
        _note(MailSearchState.natural_chosen, NATURAL_IS_INTERPRETED),
        _note(MailSearchState.understood != "", MailSearchState.understood),
        gap=LABEL_GAP,
    )


def _people() -> rx.Component:
    """Who it came from and who it went to — the archive's own addresses.

    Pickers rather than text boxes, and that is a change of promise as much as
    of control: what the archive holds is what can be asked for, so a filter
    cannot be typed that no message could ever match. Both lists come out of
    the graph when the page loads, each in the role it is offered for.

    Both take several addresses, and only **To** carries a switch over them.
    That asymmetry is the data's rather than the design's: a message has one
    sender and any number of recipients, so *from all of these* is a question
    with no answer while *to all of these* is a useful one.

    Both are ``searchable`` because an address book is not a list anybody
    scrolls, and both are ``clearable`` because a filter that can be set has to
    be unsettable in the same gesture. Only the first
    :data:`~mailarc_ui.kit.OPTION_LIMIT` options are ever put in the DOM; the
    search is over the whole list.
    """
    return mn.stack(
        multi_select_field(
            label="From",
            hint=ANY_SENDER,
            data=MailSearchState.sender_options,
            value=MailSearchState.senders,
            on_change=MailSearchState.choose_senders,
            disabled=MailSearchState.semantic_chosen,
            placeholder="any sender",
            searchable=True,
            clearable=True,
            hide_picked_options=True,
            limit=OPTION_LIMIT,
            nothing_found_message=NOT_ARCHIVED,
        ),
        multi_select_field(
            label="To",
            hint=_recipient_match(),
            data=MailSearchState.recipient_options,
            value=MailSearchState.recipients,
            on_change=MailSearchState.choose_recipients,
            disabled=MailSearchState.semantic_chosen,
            placeholder="any recipient",
            searchable=True,
            clearable=True,
            hide_picked_options=True,
            limit=OPTION_LIMIT,
            nothing_found_message=NOT_ARCHIVED,
        ),
        gap=FIELD_GAP,
    )


def _recipient_match() -> rx.Component:
    """The switch across from the **To** label: *all* of them, or *any*.

    In the label's hint slot rather than in a row of its own, because it is
    not a field — nothing is collected by it — and because putting it there
    is what makes it read as belonging to the picker under it. See
    :data:`MATCH_SEGMENTS`.
    """
    return range_switch(
        data=MATCH_SEGMENTS,
        value=MailSearchState.recipient_match,
        on_change=MailSearchState.choose_recipient_match,
        disabled=MailSearchState.semantic_chosen,
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


def _file_types() -> rx.Component:
    """Which kinds of file the message has to carry — any of the picked ones.

    Beside the tri-state above it rather than folded into it: *has files* and
    *has a spreadsheet* are two questions, and a segmented control with a
    dropdown grafted onto one of its three positions would answer neither
    clearly. Picking a family here already implies "with attachments", which
    is why nothing forces the segment above.

    Offered only where there is anything to offer: an archive whose messages
    carry no files at all shows no picker rather than an empty one — see
    :attr:`~mailarc_ui.search.state.MailSearchState.file_type_options`.

    The **To** picker's control, with the **To** picker's props: several
    families are picked as pills, typing narrows the list, and a word nothing
    answers says so in the same sentence an unarchived address gets. A field
    that behaves like the one above it is one fewer thing to learn.
    """
    return rx.cond(
        MailSearchState.file_type_options,
        multi_select_field(
            label="File type",
            data=MailSearchState.file_type_options,
            value=MailSearchState.file_kinds,
            on_change=MailSearchState.choose_file_kinds,
            disabled=MailSearchState.semantic_chosen,
            placeholder="any file",
            searchable=True,
            clearable=True,
            hide_picked_options=True,
            limit=OPTION_LIMIT,
            nothing_found_message=NOT_ARCHIVED,
        ),
        rx.fragment(),
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
    return rx.cond(when, field_note(text), rx.fragment())


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
            _file_types(),
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
