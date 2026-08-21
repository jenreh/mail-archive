"""The accounts page, in the plainest components that do the job.

Phase 4 is deliberately ugly (§10) and this is the ugly part: a table, a form
and four buttons. Only one thing here is meant to outlive it — the form fields
come from :attr:`MailAccountState.credential_fields` through ``rx.foreach``, so
a provider that declares three fields renders three, and nothing in this file
knows what any of them are called.

Every component is a function of state vars and event handlers. None of them
builds a repository, reads configuration or touches a session; that all lives
behind the state.

The ``lambda`` around a handler is how a row passes its own id into one, and
``ty`` cannot model that call — hence the suppressions, the same ones
``app/pages/home.py`` carries.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.accounts.state import AccountRow, CredentialInput, MailAccountState


def _credential_input(field: CredentialInput) -> rx.Component:
    """One generated field: masked where the provider said it is a secret."""
    return rx.cond(
        field.secret,
        mn.password_input(
            label=field.label,
            placeholder=field.placeholder,
            required=field.required,
            default_value="",
            on_change=lambda value: MailAccountState.set_credential(field.name, value),  # ty: ignore[invalid-argument-type]
        ),
        mn.text_input(
            label=field.label,
            placeholder=field.placeholder,
            required=field.required,
            default_value="",
            on_change=lambda value: MailAccountState.set_credential(field.name, value),  # ty: ignore[invalid-argument-type]
        ),
    )


def _account_row(row: AccountRow) -> rx.Component:
    return mn.table.tr(
        mn.table.td(row.provider),
        mn.table.td(row.display_name),
        mn.table.td(row.email_address),
        mn.table.td(
            mn.stack(
                mn.badge(row.status, variant="light", size="sm"),
                rx.cond(
                    row.last_error != "",
                    mn.text(row.last_error, size="xs", c="red"),
                    mn.text(""),
                ),
                gap=4,
            ),
        ),
        mn.table.td(
            mn.group(
                mn.button(
                    "Connect",
                    variant="light",
                    size="xs",
                    disabled=MailAccountState.busy,
                    on_click=lambda: MailAccountState.start_consent(row.id),  # ty: ignore[invalid-argument-type]
                ),
                mn.button(
                    "Delete",
                    variant="light",
                    color="red",
                    size="xs",
                    disabled=MailAccountState.busy,
                    on_click=lambda: MailAccountState.delete_account(row.id),  # ty: ignore[invalid-argument-type]
                ),
                gap="xs",
            ),
        ),
    )


def error_alert() -> rx.Component:
    """Whatever went wrong last, in the words the state kept."""
    return rx.cond(
        MailAccountState.error != "",
        mn.alert(
            MailAccountState.error,
            title="That did not work",
            color="red",
            variant="light",
            icon=rx.icon("triangle-alert", size=16),
        ),
        mn.text(""),
    )


def add_account_form() -> rx.Component:
    """Provider, address, and whatever that provider declared it needs."""
    return mn.card(
        mn.stack(
            mn.text("Add an account", fw=600, size="sm"),
            mn.select(
                label="Provider",
                placeholder="Pick a provider",
                data=MailAccountState.provider_options,
                value=MailAccountState.provider,
                on_change=MailAccountState.select_provider,
            ),
            mn.text_input(
                label="Email address",
                placeholder="you@example.com",
                required=True,
                default_value="",
                on_change=MailAccountState.set_email_address,
            ),
            mn.text_input(
                label="Name",
                placeholder="What to call this mailbox",
                default_value="",
                on_change=MailAccountState.set_display_name,
            ),
            rx.foreach(MailAccountState.credential_fields, _credential_input),
            mn.button(
                "Add account",
                on_click=MailAccountState.create_account,
                loading=MailAccountState.busy,
                w="fit-content",
            ),
            gap="sm",
        ),
        shadow="sm",
        padding="lg",
        radius="md",
        with_border=True,
        w="100%",
    )


def accounts_table() -> rx.Component:
    """The mailboxes this archive knows, or the reason there are none yet."""
    return mn.card(
        mn.stack(
            mn.text("Accounts", fw=600, size="sm"),
            rx.cond(
                MailAccountState.has_accounts,
                mn.table(
                    mn.table.thead(
                        mn.table.tr(
                            mn.table.th("Provider"),
                            mn.table.th("Name"),
                            mn.table.th("Address"),
                            mn.table.th("Status"),
                            mn.table.th(""),
                        ),
                    ),
                    mn.table.tbody(
                        rx.foreach(MailAccountState.accounts, _account_row),
                    ),
                    striped=True,
                    highlight_on_hover=True,
                ),
                mn.empty_state(
                    icon=rx.icon("mail", size=28),
                    title="No accounts yet",
                    description="Add one above, then connect it.",
                    align="center",
                ),
            ),
            gap="sm",
        ),
        shadow="sm",
        padding="lg",
        radius="md",
        with_border=True,
        w="100%",
    )


def accounts_panel() -> rx.Component:
    """Everything above, in the order a page wants it."""
    return mn.stack(
        error_alert(),
        add_account_form(),
        accounts_table(),
        gap="lg",
        w="100%",
    )
