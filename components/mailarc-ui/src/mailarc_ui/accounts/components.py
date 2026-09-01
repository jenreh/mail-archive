"""The accounts screen: the mailboxes on the left, the chosen one on the right.

Two columns, the same two the search page has — a list
at :data:`~mailarc_ui.kit.LIST_WIDTH` and, beside it, the thing that list
selects. It replaces a stack of three full-width cards, and what changes is
not the styling: a table of mailboxes with a Connect and a Delete button in
every row made the page a list of *rows to act on*, while a mailbox is a thing
with a state, a history and two actions. The column is where those fit.

Layout and nothing else. Every value comes from :class:`MailAccountState` and
every piece is a ``kit`` component, so the list here and the mail list on the
search page are the same list drawn from the same vocabulary.

Only one thing in this module knows about a provider, and it knows it by
declaration: the form fields come from
:attr:`MailAccountState.credential_fields` through ``rx.foreach``, so a
provider that declares three fields renders three, and nothing here learns
what any of them are called.

The ``lambda`` around a handler is how a row passes its own id into one, the
way every list in this application does it.
"""

from typing import Any, cast

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.accounts.state import (
    EMAIL_FIELD,
    PROVIDER_FIELD,
    AccountRow,
    CredentialInput,
    MailAccountState,
)
from mailarc_ui.kit import (
    FIELD_GAP,
    LIST_WIDTH,
    avatar_initials,
    column_card,
    empty_panel,
    field_note,
    input_field,
    list_row,
    message,
    password_field,
    primary_button,
    quiet_button,
    select_field,
    soft_button,
    status_badge,
)
from mailarc_ui.message_detail import COLUMN, ROW_BORDER

AVATAR_SIZE = 44
"""The initials circle in the detail column — the reading-pane size."""

FACT_LABEL_WIDTH = 72
"""Wide enough for ``Provider``, so the two values line up under each other."""

REQUIRED_HINT = "required"
"""What a field that must be filled says across from its own label."""


def _open() -> AccountRow:
    """The mailbox the detail column is showing.

    What crosses to the browser is a ``ComputedVar``, and ``ty`` cannot see a
    field through one — every ``MailAccountState.selected.status`` would need a
    suppression of its own. Stated once here instead: at runtime the Var
    proxies the row it produces, which is exactly what the cast says.
    """
    return cast(AccountRow, MailAccountState.selected)


def _status_badge(
    status: rx.Var | str,
    color: rx.Var | str,
    size: str = "xs",
) -> rx.Component:
    """What the archive last knew about this mailbox.

    Takes the two values rather than the row, so that the list — which has an
    ``AccountRow`` — and the detail column — which has a computed `Var` of one
    — can both call it without either of them naming a colour. The colour
    travels *on* the row for the reason :mod:`mailarc_ui.accounts.state` gives
    where it keeps the table: matching a status here would mean switching on a
    `Var`.
    """
    return status_badge(status, color, size=size)


def _account_row(row: AccountRow, on_select: Any) -> rx.Component:
    """One mailbox in the list: who it is, and where it stands.

    Two lines and a badge. What the old table showed — provider, the last
    error, the buttons — belongs to the mailbox that is open, not to every
    mailbox at once, and the column beside this one is where it went.
    """
    return list_row(
        avatar_initials(row.display_name),
        mn.stack(
            mn.group(
                mn.text(
                    row.display_name,
                    size="md",
                    fw=600,
                    truncate="end",
                    style={"minWidth": 0},
                ),
                _status_badge(row.status, row.status_color),
                gap="xs",
                align="center",
                wrap="nowrap",
                justify="space-between",
                w="100%",
            ),
            mn.text(
                row.email_address,
                size="sm",
                c="dimmed",
                truncate="end",
                w="100%",
            ),
            gap=2,
            style={"minWidth": 0, "flex": "1 1 auto"},
        ),
        selected=MailAccountState.selected_id == row.id,
        on_click=lambda: _select_events(row, on_select),
        w="100%",
    )


def _select_events(row: AccountRow, on_select: Any) -> list[Any]:
    """Opening a mailbox, plus whatever else the page wants that click to do.

    The page hands in
    :meth:`~mailarc_ui.imports.state.ImportJobState.select_account`, so one
    click both opens a mailbox and points the import panel at it while the two
    states still know nothing of each other.
    """
    events: list[Any] = [MailAccountState.select(row.id)]
    if on_select is not None:
        events.append(on_select(row.id))
    return events


def _list_strip() -> rx.Component:
    """How many mailboxes there are, and the way to add another.

    The same strip the two mail lists carry, and the only place ``Add`` lives:
    the detail column shows a mailbox *or* the form for a new one, so this
    button is what clears the selection.
    """
    return mn.group(
        mn.text(
            MailAccountState.count_label,
            size="xs",
            c="dimmed",
            class_name="ma-tabular",
        ),
        quiet_button(
            "Add",
            on_click=MailAccountState.start_new,
            left_section=rx.icon("plus", size=14),
            size="xs",
        ),
        justify="space-between",
        align="center",
        px="md",
        py="xs",
        w="100%",
        style={"borderBottom": ROW_BORDER, "flexShrink": 0},
    )


def _nothing_yet() -> rx.Component:
    """An archive with no mailbox in it, which is where everybody starts."""
    return empty_panel(
        "mail",
        "No mailboxes yet",
        "Add one, connect it, then import it.",
    )


def accounts_list(on_select: Any = None) -> rx.Component:
    """The left column: every mailbox this archive knows, scrolling on its own.

    ``on_select`` is an event handler taking an account id, fired beside
    :meth:`MailAccountState.select` — see :func:`_select_events`.
    """
    return column_card(
        mn.stack(
            _list_strip(),
            mn.box(
                rx.cond(
                    MailAccountState.has_accounts,
                    mn.scroll_area(
                        mn.stack(
                            rx.foreach(
                                MailAccountState.accounts,
                                lambda row: _account_row(row, on_select),
                            ),
                            gap=0,
                        ),
                        type="hover",
                        offset_scrollbars=False,
                        style={"height": "100%"},
                    ),
                    _nothing_yet(),
                ),
                style=COLUMN,
            ),
            gap=0,
            h="100%",
            w="100%",
        ),
        w=LIST_WIDTH,
        style={"flexShrink": 0},
    )


def error_alert() -> rx.Component:
    """Whatever went wrong last, in the words the state kept."""
    return rx.cond(
        MailAccountState.error != "",
        message(MailAccountState.error, "failure", title="That did not work"),
        rx.fragment(),
    )


def _fact(label: str, value: rx.Var | str) -> rx.Component:
    """One ``Provider: gmail`` line of the detail header."""
    return mn.group(
        mn.text(
            label,
            size="sm",
            c="dimmed",
            w=FACT_LABEL_WIDTH,
            style={"flexShrink": 0},
        ),
        mn.text(value, size="sm", style={"wordBreak": "break-word"}),
        gap="xs",
        align="flex-start",
        wrap="nowrap",
        w="100%",
    )


def _account_error() -> rx.Component:
    """The mailbox's own last failure, which outlives the page that saw it.

    Distinct from :func:`error_alert`: that one reports what the *click* did,
    this one reports what the archive recorded — an expired token still shows
    on a page nobody has touched.
    """
    return rx.cond(
        _open().last_error != "",
        message(_open().last_error, "failure", title="This mailbox last reported"),
        rx.fragment(),
    )


def _cleared_notice() -> rx.Component:
    """What the last clear-out removed, in the words the state counted.

    Its own alert rather than a line in :func:`error_alert`: a clear-out that
    worked is not an error, and the count is the only confirmation there is —
    the mailbox stays in the list and looks exactly as it did.
    """
    return rx.cond(
        MailAccountState.cleared != "",
        message(MailAccountState.cleared, "success", title="Mailbox cleared"),
        rx.fragment(),
    )


def clear_confirmation() -> rx.Component:
    """The dialog that stands between a click and an emptied mailbox.

    An ``alert_dialog`` and not a ``modal``: it exists to be acknowledged, and
    the mailbox it names is written into the sentence so that confirming is a
    decision about *this* mailbox rather than about whatever was open.

    What the sentence promises is what :meth:`MailAccountState.clear_account`
    does — the mailbox and its credential survive, the import does not — and
    it says the import can be run again, because that is the reason to reach
    for this rather than for Delete.
    """
    return mn.alert_dialog.root(
        mn.alert_dialog.content(
            mn.title("Clear this mailbox?", order=4),
            mn.text(
                "Everything imported from "
                + _open().email_address
                + " is deleted from the archive. The mailbox, its connection "
                "and its settings stay, so it can be imported again from the "
                "beginning.",
                size="sm",
                c="dimmed",
            ),
            mn.text(
                "Mail that another mailbox also holds stays in the archive.",
                size="sm",
                c="dimmed",
            ),
            mn.alert_dialog.footer(
                cancel_label="Cancel",
                action_label="Clear mailbox",
                action_loading=MailAccountState.clearing,
                on_cancel=MailAccountState.cancel_clear,
                on_action=MailAccountState.clear_account,
            ),
        ),
        open=MailAccountState.confirming_clear,
        on_open_change=MailAccountState.set_confirming_clear,
    )


def account_actions() -> rx.Component:
    """The three things that can be done to the open mailbox.

    Connect is the primary one and the reason the page exists after the first
    day: a token expires, and re-consent is the whole repair. Clear and Delete
    are beside it rather than in the list, where a stray click would have been
    one row away from the wrong mailbox.

    Clear and Delete look alike and are not: clearing empties an import so it
    can be run again, deleting forgets the mailbox. So only the irreversible
    one is red, and the reversible one asks first — the dialog is where the
    difference is spelled out.
    """
    return mn.group(
        primary_button(
            "Connect",
            left_section=rx.icon("link", size=14),
            disabled=MailAccountState.busy | MailAccountState.clearing,
            on_click=lambda: MailAccountState.start_consent(
                MailAccountState.selected_id
            ),
        ),
        quiet_button(
            "Clear",
            left_section=rx.icon("eraser", size=14),
            loading=MailAccountState.clearing,
            disabled=MailAccountState.busy,
            on_click=MailAccountState.ask_clear,
        ),
        soft_button(
            "Delete",
            color="red",
            left_section=rx.icon("trash-2", size=14),
            disabled=MailAccountState.busy | MailAccountState.clearing,
            on_click=lambda: MailAccountState.delete_account(
                MailAccountState.selected_id
            ),
        ),
        clear_confirmation(),
        gap="xs",
        align="center",
        w="100%",
    )


def _identity_fields() -> rx.Component:
    """Address and name — the two fields both halves of this column write.

    Controlled, unlike the credential boxes below them. The edit half has to
    *show* what a mailbox is called before anybody can correct it, and a value
    the state holds is the only thing that refills a box when another mailbox
    is opened — an uncontrolled input keeps whatever was typed into it, no
    matter what the state says.
    """
    return mn.stack(
        input_field(
            label="Email address",
            hint=REQUIRED_HINT,
            placeholder="you@example.com",
            required=True,
            value=MailAccountState.email_address,
            on_change=MailAccountState.set_email_address,
            error=MailAccountState.errors[EMAIL_FIELD],
        ),
        input_field(
            label="Name",
            placeholder="What to call this mailbox",
            value=MailAccountState.display_name,
            on_change=MailAccountState.set_display_name,
        ),
        gap=FIELD_GAP,
        w="100%",
    )


def _stored_credentials() -> rx.Component:
    """The secret a mailbox already has: never shown, replaced on purpose.

    Nothing on this page can display a credential — the archive decrypts one
    only to open a mailbox — so the honest offer is to replace it. Until
    somebody asks, the fields are not on the page at all, and that is what
    keeps them truthful: a box that stayed mounted would carry what was typed
    for one mailbox into the next one, showing text this page would not save.
    """
    return rx.cond(
        MailAccountState.replacing_credentials,
        mn.stack(
            rx.foreach(MailAccountState.credential_fields, _credential_input),
            field_note("Saving replaces the stored credential for this mailbox."),
            gap=FIELD_GAP,
            w="100%",
        ),
        mn.group(
            mn.text("Credentials are stored and not shown.", size="sm", c="dimmed"),
            quiet_button(
                "Replace",
                left_section=rx.icon("key-round", size=14),
                size="xs",
                on_click=MailAccountState.replace_credentials,
            ),
            justify="space-between",
            align="center",
            wrap="nowrap",
            w="100%",
        ),
    )


def account_settings() -> rx.Component:
    """The open mailbox's own values, editable, and the button that writes them.

    Not the provider: the stored credential and everything already imported
    belong to *that* provider, so a mailbox that changed provider is a new
    mailbox rather than an edited one. It stays a fact in the header.

    **The button is not disabled by ``has_errors``**, and that was a bug before
    it was a decision. Not every field on this form complains as it is typed
    into: a credential box the provider declared is only checked once somebody
    touches it, so an untouched required one has nothing under it until a press
    asks. Disabling the press on the first complaint meant the second one could
    never be reached — the form said "the address is required", went dead, and
    never mentioned the empty credential beside it. Pressing is how a person
    asks what is wrong; the *state* is what refuses to write.
    """
    return mn.stack(
        _identity_fields(),
        rx.cond(
            MailAccountState.has_credential_fields,
            _stored_credentials(),
            rx.fragment(),
        ),
        primary_button(
            "Save changes",
            left_section=rx.icon("check", size=14),
            on_click=MailAccountState.save_account,
            loading=MailAccountState.busy,
            disabled=MailAccountState.clearing,
            w="fit-content",
        ),
        gap=FIELD_GAP,
        w="100%",
    )


def account_detail() -> rx.Component:
    """The open mailbox: who it is, where it stands, and what can be done to it."""
    return mn.stack(
        mn.group(
            avatar_initials(_open().display_name, size=AVATAR_SIZE),
            mn.stack(
                mn.text(
                    _open().display_name,
                    size="lg",
                    fw=600,
                    truncate="end",
                    w="100%",
                ),
                mn.text(
                    _open().email_address,
                    size="sm",
                    c="dimmed",
                    truncate="end",
                    w="100%",
                ),
                gap=2,
                style={"minWidth": 0, "flex": "1 1 auto"},
            ),
            _status_badge(_open().status, _open().status_color, size="sm"),
            gap="md",
            align="center",
            wrap="nowrap",
            w="100%",
        ),
        _fact("Provider", _open().provider),
        _account_error(),
        _cleared_notice(),
        account_settings(),
        mn.divider(),
        account_actions(),
        gap="md",
        w="100%",
    )


def _credential_input(field: CredentialInput) -> rx.Component:
    """One generated field: masked where the provider said it is a secret.

    ``hint`` rather than Mantine's asterisk, because the label over a kit
    field is this design's own: ``required`` still reaches the control, but
    nothing draws it, and a field that stopped saying it is required is a form
    people submit twice.
    """
    hint = rx.cond(field.required, REQUIRED_HINT, "")
    return rx.cond(
        field.secret,
        password_field(
            label=field.label,
            hint=hint,
            placeholder=field.placeholder,
            required=field.required,
            default_value="",
            on_change=lambda value: MailAccountState.set_credential(field.name, value),
            error=MailAccountState.errors[field.name],
        ),
        input_field(
            label=field.label,
            hint=hint,
            placeholder=field.placeholder,
            required=field.required,
            default_value="",
            on_change=lambda value: MailAccountState.set_credential(field.name, value),
            error=MailAccountState.errors[field.name],
        ),
    )


def add_account_form() -> rx.Component:
    """Provider, address, and whatever that provider declared it needs.

    No card of its own any more: it fills the detail column, and a card inside
    a column would draw a second surface on a surface.
    """
    return mn.stack(
        mn.stack(
            mn.text("Add a mailbox", fw=600, size="lg"),
            mn.text(
                "Pick a provider, name the mailbox, and connect it afterwards.",
                size="sm",
                c="dimmed",
            ),
            gap=4,
        ),
        select_field(
            label="Provider",
            placeholder="Pick a provider",
            data=MailAccountState.provider_options,
            value=MailAccountState.provider,
            on_change=MailAccountState.select_provider,
            error=MailAccountState.errors[PROVIDER_FIELD],
        ),
        _identity_fields(),
        rx.foreach(MailAccountState.credential_fields, _credential_input),
        primary_button(
            "Add mailbox",
            on_click=MailAccountState.create_account,
            loading=MailAccountState.busy,
            w="fit-content",
        ),
        gap=FIELD_GAP,
        w="100%",
    )
