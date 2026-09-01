"""Add a mailbox, connect it, import it, watch the import run.

Two columns, the same pair every other screen in this archive opens with: the
mailboxes on the left at :data:`~mailarc_ui.kit.LIST_WIDTH`, and on the right
whichever one of them is selected — its form, its two actions, and the import
running against it.

On ``mailarc_full_app`` for the reason the search page is: each column scrolls
on its own, which needs a parent with a definite height rather than one that
grows to fit them.

Layout and nothing else, and this page has one more layout decision than most:
the right column shows a mailbox *or* the form for a new one, never both, so
selecting nothing is what asks for the form. It is also where two states that
know nothing of each other are introduced —
:meth:`~mailarc_ui.imports.state.ImportJobState.select_account` rides along
with the click that opens a mailbox, so the import panel acts on the mailbox
that is on screen and neither state has to learn the other's name.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.accounts import (
    MailAccountState,
    account_detail,
    accounts_list,
    add_account_form,
    error_alert,
)
from mailarc_ui.imports import ImportJobState, import_panel
from mailarc_ui.kit import COLUMN_GAP, PAGE_INSET, column_card
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_full_app, public_page

ROUTE = routes.ACCOUNTS
"""Where this page lives; the rail reads the same constant."""

DETAIL_WIDTH = 720
"""How wide the detail column's content grows before it stops.

A labelled field spanning the whole of a wide window is a field nobody can
follow from its label to its box. The column itself takes the rest of the
page — what is capped is the reading measure inside it, the way a settings
page is laid out rather than a reading pane.
"""


def _detail_column() -> rx.Component:
    """The right column: the open mailbox, or how to add one.

    Scrolls inside its own edge, and pads inside the scroll area rather than
    on the card, so the scrollbar sits against the border instead of floating
    in a margin.
    """
    return column_card(
        mn.scroll_area(
            mn.stack(
                error_alert(),
                rx.cond(
                    MailAccountState.has_selection,
                    mn.stack(
                        account_detail(),
                        mn.divider(),
                        import_panel(),
                        gap="lg",
                        w="100%",
                    ),
                    add_account_form(),
                ),
                gap="lg",
                p="lg",
                w="100%",
                maw=DETAIL_WIDTH,
            ),
            type="hover",
            offset_scrollbars=False,
            style={"height": "100%"},
        ),
        style={"minWidth": 0},
    )


@public_page(
    route=ROUTE,
    title="Mail accounts",
    description="Add a mailbox, connect it, and import it into the archive",
    template=mailarc_full_app,
    on_load=[MailAccountState.load, ImportJobState.refresh],
)
def accounts_page() -> rx.Component:
    return mn.flex(
        accounts_list(on_select=ImportJobState.select_account),
        _detail_column(),
        gap=COLUMN_GAP,
        h="100%",
        w="100%",
        align="stretch",
        p=PAGE_INSET,
    )
