"""Mail accounts in the browser: the state, and the pieces a page arranges.

``state`` owns everything that touches the database or the provider registry;
``components`` owns everything that renders. A page in ``app/pages/`` imports
from here and composes — it builds nothing itself, and this package imports
nothing from the application.
"""

from mailarc_ui.accounts.components import (
    accounts_panel,
    accounts_table,
    add_account_form,
    error_alert,
)
from mailarc_ui.accounts.state import (
    AccountRow,
    CredentialInput,
    MailAccountState,
    provider_registry,
)

__all__ = [
    "AccountRow",
    "CredentialInput",
    "MailAccountState",
    "accounts_panel",
    "accounts_table",
    "add_account_form",
    "error_alert",
    "provider_registry",
]
