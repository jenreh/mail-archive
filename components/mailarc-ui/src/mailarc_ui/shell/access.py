"""Who is asking, decided once for every state that has to know.

**A page decorator is not an access control.** ``admin_only=True`` folds into a
render-time ``rx.cond``, and appkit's ``_build_auth_handlers`` runs the rest of
an ``on_load`` chain whatever ``LoginState.check_auth`` returned. Worse, a
Reflex event handler is reached by **name over the websocket** and never by
route: the event processor looks the handler up in the registered-handler table
and runs it, and nothing on that path consults which page the client claims to
be on. So every handler that ships archive data or changes it has to decide for
itself, and ``/`` being public is what makes an anonymous websocket session an
ordinary thing rather than a curiosity.

Two functions, and the split is the test seam. :func:`signed_in_user` is the
three lines that need a running Reflex app; :func:`granted` is the decision, and
it is what every gate in this package is tested through. A state keeps its own
``_current_user`` wrapper so a test can replace it per state without reaching
across modules.

**Fails closed.** A process with no ``appkit_user`` in it, or one where the
session cannot be read at all — which is exactly what an anonymous visitor
produces, because ``get_state`` wants an ``EventContext`` they never
established — cannot say this caller is an administrator, and "cannot tell" is
not "yes" for a handler quoting somebody's private mail.
"""

import logging
from collections.abc import Awaitable, Callable

import reflex as rx

logger = logging.getLogger(__name__)

Asker = Callable[[], Awaitable[object | None]]
"""How a state offers up whoever is signed in, so the decision can be tested."""


async def granted(who: Asker, *, what: str, level: int = logging.WARNING) -> bool:
    """Whether the caller behind *who* may be handed administrative data.

    *what* names the read or the write in the log line, so a refusal says which
    handler refused rather than only that one did. *level* is how loudly: a
    refused write is worth a warning, and the public dashboard's own split is
    the ordinary state of a page anybody may open, so it asks for ``DEBUG``
    rather than filling a log with the fact that visitors visit.
    """
    try:
        user = await who()
    except Exception:
        logger.exception("Could not establish who is asking; refusing %s", what)
        return False
    if user is None or not getattr(user, "is_admin", False):
        logger.log(level, "Refused %s: not an administrator", what)
        return False
    return True


async def signed_in_user(state: rx.State) -> object | None:  # pragma: no cover
    """The signed-in user of *state*'s session, or ``None``.

    Excluded from coverage for the reason it is a function at all: ``get_state``
    needs an ``EventContext`` context variable that only a running Reflex app
    sets, so these three lines cannot run under pytest. Everything a gate
    actually decides sits in :func:`granted` and is covered there.

    Imported inside the function because ``appkit_user.authentication.states``
    reads its configuration out of the service registry *at import*, which a
    component's own test suite has no reason to have populated.
    """
    from appkit_user.authentication.states import UserSession

    session = await state.get_state(UserSession)
    return session.user
