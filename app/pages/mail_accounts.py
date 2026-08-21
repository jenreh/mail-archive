"""Add a mailbox, connect it, import it, watch the import run.

Layout and nothing else. Both halves are components ``mailarc-ui`` exports —
:func:`accounts_panel` owns the mailboxes, :func:`import_panel` owns the job —
and this module only decides in which order they appear and which mailbox the
import acts on. Phase 4 is deliberately ugly (§10); what stands here is meant
to be replaced, not extended.
"""

from typing import Literal

import appkit_mantine as mn
import reflex as rx
from appkit_user.authentication.templates import authenticated

from app.components.navbar import app_navbar
from mailarc_ui.accounts import AccountRow, MailAccountState, accounts_panel
from mailarc_ui.imports import ImportJobState, import_panel

ROUTE = "/mail/accounts"
"""Where this page lives.

A constant because ``app/components/navbar.py`` links here and a typo in either
half is a 404 nobody notices until a human clicks it; a test holds the two
together.
"""


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


@authenticated(
    route=ROUTE,
    title="Mail accounts",
    description="Add a mailbox, connect it, and import it into the archive",
    navbar=app_navbar(),
    with_header=False,
    # Admin-only, like `/admin/users`. `mail_accounts` carries no owner column,
    # so this page lists, connects, imports and deletes *every* mailbox in the
    # installation — on a deployment with more than one login, leaving it at
    # plain `@authenticated` would hand any account holder the whole archive,
    # which is everybody's private mail. On the desktop the only user is the
    # admin and nothing changes. An owner column is the real fix and belongs
    # with a multi-user story, not with a page §10 calls throwaway.
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positives.
    on_load=[MailAccountState.load, ImportJobState.refresh],  # ty: ignore[invalid-argument-type]
)
def mail_accounts_page() -> rx.Component:
    return mn.stack(
        mn.stack(
            mn.title("Mail accounts", order=1),
            mn.text(
                "A mailbox is added here, connected once, and then imported. "
                "The archive keeps what an import wrote, so a second run only "
                "picks up what is new.",
                c="dimmed",
                size="sm",
            ),
            gap="xs",
        ),
        accounts_panel(),
        _mailbox_picker(),
        import_panel(),
        gap="lg",
        w="100%",
        maw=900,
        mx="auto",
        p="2rem",
    )
