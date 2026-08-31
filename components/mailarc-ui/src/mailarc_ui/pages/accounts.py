"""Add a mailbox, connect it, import it, watch the import run.

Layout and nothing else. Both halves are components ``mailarc-ui`` exports —
:func:`accounts_panel` owns the mailboxes, :func:`import_panel` owns the job —
and this module only decides in which order they appear and which mailbox the
import acts on.
"""

from typing import Literal

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.accounts import AccountRow, MailAccountState, accounts_panel
from mailarc_ui.imports import ImportJobState, import_panel
from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_app, public_page

ROUTE = routes.ACCOUNTS
"""Where this page lives; the rail reads the same constant."""


def _pick_button(row: AccountRow, variant: Literal["filled", "light"]) -> rx.Component:
    """The button that hands one mailbox's id to the import panel."""
    return mn.button(
        row.email_address,
        variant=variant,
        size="xs",
        # ty cannot model reflex event-handler calls; suppress the false positive.
        on_click=lambda: ImportJobState.select_account(row.id),  # ty: ignore[invalid-argument-type]
    )


def _mailbox_button(row: AccountRow) -> rx.Component:
    """One mailbox to import from, filled in while it is the chosen one.

    Two buttons rather than a conditional prop: ``rx.cond`` in a prop hands the
    component a `Var` the type checker cannot match against a string, and the
    accounts form solves the same problem the same way.
    """
    return rx.cond(
        ImportJobState.account_id == row.id,
        _pick_button(row, "filled"),
        _pick_button(row, "light"),
    )


def _mailbox_picker() -> rx.Component:
    """Which mailbox the import panel acts on.

    The two states know nothing of each other — one lists mailboxes, the other
    wants an id — and a page is where two components are introduced. Nothing
    shows until there is an account, because there is nothing to pick.
    """
    return rx.cond(
        MailAccountState.has_accounts,
        mn.group(
            mn.text("Import from", size="sm", fw=600),
            rx.foreach(MailAccountState.accounts, _mailbox_button),
            gap="xs",
            align="center",
            w="100%",
        ),
        mn.text(""),
    )


@public_page(
    route=ROUTE,
    title="Mail accounts",
    description="Add a mailbox, connect it, and import it into the archive",
    template=mailarc_app,
    on_load=[MailAccountState.load, ImportJobState.refresh],
)
def accounts_page() -> rx.Component:
    return mn.stack(
        page_header(
            "Mail accounts",
            "A mailbox is added here, connected once, and then imported. "
            "The archive keeps what an import wrote, so a second run only "
            "picks up what is new.",
        ),
        accounts_panel(),
        _mailbox_picker(),
        import_panel(),
        gap=PAGE_GAP,
        w="100%",
        maw=900,
        mx="auto",
        p=PAGE_PADDING,
    )
