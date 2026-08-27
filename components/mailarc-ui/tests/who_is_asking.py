"""Saying who is asking, for a state with no Reflex app under it.

Every gate in ``mailarc_ui`` runs through
:func:`mailarc_ui.shell.access.granted`, which is handed the state's own
``_current_user``. That method goes through ``self.get_state(UserSession)``, and
``get_state`` needs an ``EventContext`` context variable that only a running
Reflex app sets — which is exactly why it is a method of its own. Everything a
gate actually *decides* sits above it and is tested through these two helpers.

One module rather than a copy per test file: two of them had already drifted
into three definitions of ``FakeUser``, and a gate that is tested three
slightly different ways is a gate nobody can read.
"""

from typing import Any

import pytest


class FakeUser:
    """Just the one attribute a gate reads off a signed-in user."""

    def __init__(self, is_admin: bool) -> None:
        self.is_admin = is_admin


def signed_in_as(
    state: Any, user: FakeUser | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answer ``_current_user`` with *user* — ``None`` for nobody signed in."""

    async def current(_self: object) -> FakeUser | None:
        return user

    monkeypatch.setattr(type(state), "_current_user", current)


def nobody_can_be_established(state: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The anonymous case as it actually arrives: no session to read at all.

    ``get_state`` raises without an ``EventContext``, and a visitor with no
    session never establishes one — so what a gate meets is an exception rather
    than a ``None`` user. That is the branch that has to fail closed, and
    mocking it away with ``None`` would leave it untested.
    """

    async def current(_self: object) -> FakeUser | None:
        raise LookupError("no event context — nobody is asking")

    monkeypatch.setattr(type(state), "_current_user", current)
