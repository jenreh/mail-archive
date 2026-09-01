"""Mail accounts in the browser: the state, and the pieces a page arranges.

``state`` owns everything that touches the database or the provider registry;
``components`` owns everything that renders. A page in ``mailarc_ui.pages``
imports from here and composes — it builds nothing itself, and this package
imports nothing from the application.
"""

from mailarc_ui.accounts.components import (
    account_actions,
    account_detail,
    account_settings,
    accounts_list,
    add_account_form,
    clear_confirmation,
    error_alert,
)
from mailarc_ui.accounts.state import (
    AccountRow,
    CredentialInput,
    MailAccountState,
    account_eraser,
    provider_registry,
)

__all__ = [
    "AccountRow",
    "CredentialInput",
    "MailAccountState",
    "account_actions",
    "account_detail",
    "account_eraser",
    "account_settings",
    "accounts_list",
    "add_account_form",
    "clear_confirmation",
    "error_alert",
    "provider_registry",
]
