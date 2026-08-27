"""Signing in, and getting back in after forgetting how.

Three pages this application does not write. appkit builds them from factories
rather than registering them on import, so they are the one part of the
interface that could not simply move into a page module — and while the call
sat in ``app/app.py``, that file was the only one left holding a page
registration of its own.

:func:`register_auth_pages` is where it went. The three pages stay appkit's:
its login form, its password rules, its routes. What this module adds is the
one thing a factory cannot have, which is a memory of having been called —
``rx.page`` appends to a module-level registry, so a second call registers a
second page at ``/login`` and Reflex picks one of them without saying which.
That is not hypothetical on a framework that reloads modules.

The two OAuth callback pages are here for the same reason in a different
shape: they register on *import*, so importing this module is what puts them
in the table. They are named in ``__all__`` so that saying so is deliberate
rather than an unused-import warning somebody eventually deletes.
"""

import logging

from appkit_user.authentication.pages import (
    azure_oauth_callback_page,
    github_oauth_callback_page,
)
from appkit_user.user_management.pages import (
    create_login_page,
    create_password_reset_confirm_page,
    create_password_reset_request_page,
)

logger = logging.getLogger(__name__)

AUTH_ROUTES: tuple[str, ...] = (
    "/login",
    "/password-reset",
    "/password-reset/confirm",
)
"""Where appkit's three pages answer.

Spelled here rather than in ``shell/routes.py`` deliberately: these are
appkit's routes and it owns them, and naming them in the archive's own table
would create a second place they could disagree. This tuple is what a test
walks, not what the application configures — the factories are called with
their defaults.
"""

_registered = False


def register_auth_pages() -> None:
    """Register the login and password-reset pages. A second call is a no-op."""
    global _registered
    if _registered:
        logger.debug("Authentication pages are already registered")
        return
    create_login_page()
    create_password_reset_request_page()
    create_password_reset_confirm_page()
    _registered = True


__all__ = [
    "AUTH_ROUTES",
    "azure_oauth_callback_page",
    "github_oauth_callback_page",
    "register_auth_pages",
]
