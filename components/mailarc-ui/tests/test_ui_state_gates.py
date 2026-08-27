"""Every handler that ships or changes archive data refuses an anonymous caller.

**A Reflex event handler is reached by name, not by route.** The event processor
looks the handler up in the registered-handler table and runs it; nothing on
that path consults which page the client claims to be on, which is why
``admin_only=True`` on a page decorator is a render-time ``rx.cond`` and not an
access control. Since ``/`` is public, an anonymous websocket session is an
ordinary thing — and one that could name ``MailAccountState.delete_account`` or
``MessageReviewState.select`` and be served.

So the claim under test is *not* "the page hides the button". It is: with
nobody signed in, the handler returns without ever reaching its collaborator.
Each collaborator is replaced with a tripwire that raises on any call, so a gate
that let the caller through fails the test at the point the read would have
happened rather than at an assertion about what came back.

The signed-in non-administrator is exercised too. On a single-user desktop the
two cases look the same; on a deployment with more than one login they are the
whole difference, and every one of these states reads or writes **every**
mailbox in the installation.
"""

from typing import Any

import pytest
from who_is_asking import FakeUser, nobody_can_be_established, signed_in_as

from mailarc_ui.accounts.state import MailAccountState
from mailarc_ui.imports.state import ImportJobState
from mailarc_ui.review.state import MessageReviewState
from mailarc_ui.status.state import GraphStatusState

ACCOUNTS = "mailarc_ui.accounts.state"
IMPORTS = "mailarc_ui.imports.state"
REVIEW = "mailarc_ui.review.state"
STATUS = "mailarc_ui.status.state"


class Tripwire(Exception):
    """Raised by a collaborator nothing was supposed to reach."""


def _tripwire(*_args: Any, **_kwargs: Any) -> Any:
    raise Tripwire("the gate let a caller through to the archive")


async def _async_tripwire(*_args: Any, **_kwargs: Any) -> Any:
    raise Tripwire("the gate let a caller through to the archive")


@pytest.fixture
def sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every collaborator these four states reach for, made into a tripwire.

    Patched where the state looks them up rather than where they are defined,
    which is the only place a state's own gate can be proven from.
    """
    for target in (
        f"{REVIEW}.archive_reader",
        f"{STATUS}.graph_health",
        f"{ACCOUNTS}.provider_registry",
        f"{IMPORTS}.JobQueue",
    ):
        monkeypatch.setattr(target, _tripwire)
    for target in (
        f"{ACCOUNTS}._read_accounts",
        f"{ACCOUNTS}._delete",
        f"{ACCOUNTS}._connect",
    ):
        monkeypatch.setattr(target, _async_tripwire)


def _visitors(state: Any, monkeypatch: pytest.MonkeyPatch, who: str) -> None:
    """Put one of the two refused callers behind *state*."""
    if who == "anonymous":
        nobody_can_be_established(state, monkeypatch)
    else:
        signed_in_as(state, FakeUser(is_admin=False), monkeypatch)


REFUSED = pytest.mark.parametrize("who", ["anonymous", "a signed-in non-admin"])


@pytest.mark.usefixtures("sealed")
@REFUSED
class TestTheGraphStatusPanel:
    """``/admin/status`` names the endpoint, the versions and the memory in use.

    The page moved onto an ``admin_only`` route in this change and the boundary
    was asserted to live in the state; these are the tests that make that true.
    """

    async def test_a_refused_caller_gets_no_reading(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = GraphStatusState()
        _visitors(state, monkeypatch, who)

        await GraphStatusState.refresh.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.checked is False
        assert state.endpoint == ""
        assert state.redis_version == ""
        assert state.graphs == []

    async def test_a_refused_caller_cannot_start_the_poll(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = GraphStatusState()
        _visitors(state, monkeypatch, who)

        started = await GraphStatusState.start_polling.fn(state)  # ty: ignore[unresolved-attribute]

        assert started is None
        assert state.polling is False

    async def test_a_refused_caller_cannot_drive_the_poll_by_name(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``poll`` is a registered handler of its own, so it is addressable
        whatever ``start_polling`` decided."""
        state = GraphStatusState()
        state.polling = True
        _visitors(state, monkeypatch, who)

        await GraphStatusState.poll.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.checked is False
        assert state.polling is False


@pytest.mark.usefixtures("sealed")
@REFUSED
class TestTheMessageReader:
    """The review page hands over subjects, addresses and message bodies."""

    async def test_a_refused_caller_gets_no_listing(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = MessageReviewState()
        _visitors(state, monkeypatch, who)

        await MessageReviewState.load.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.messages == []
        assert state.total == 0
        assert state.error == ""

    async def test_a_refused_caller_cannot_page_further(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = MessageReviewState()
        state.total = 100
        _visitors(state, monkeypatch, who)

        await MessageReviewState.load_more.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.messages == []

    async def test_a_refused_caller_cannot_open_a_message(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = MessageReviewState()
        _visitors(state, monkeypatch, who)

        await MessageReviewState.select.fn(state, "m-1")  # ty: ignore[unresolved-attribute]

        assert state.selected_id == ""
        assert state.raw == ""

    async def test_a_refused_caller_cannot_record_a_trust_decision(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write, and one that changes how somebody else's mail renders."""
        state = MessageReviewState()
        state.view = state.view.model_copy(update={"sender_address": "a@example.com"})
        _visitors(state, monkeypatch, who)

        await MessageReviewState.allow_remote_for_sender.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.remote_allowed is False


@pytest.mark.usefixtures("sealed")
@REFUSED
class TestTheMailAccounts:
    """This page lists, connects, imports and deletes *every* mailbox."""

    async def test_a_refused_caller_gets_no_mailboxes(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = MailAccountState()
        _visitors(state, monkeypatch, who)

        await MailAccountState.load.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.accounts == []
        assert state.provider_options == []
        assert state.error == ""

    async def test_a_refused_caller_cannot_create_an_account(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = MailAccountState()
        state.email_address = "someone@example.com"
        _visitors(state, monkeypatch, who)

        await MailAccountState.create_account.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.accounts == []

    async def test_a_refused_caller_cannot_delete_an_account(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one that is not a disclosure: an anonymous ``delete_account(1)``
        would take a mailbox and its stored secret with it."""
        state = MailAccountState()
        _visitors(state, monkeypatch, who)

        await MailAccountState.delete_account.fn(state, 1)  # ty: ignore[unresolved-attribute]

        assert state.busy is False

    async def test_a_refused_caller_cannot_start_a_consent_run(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = MailAccountState()
        _visitors(state, monkeypatch, who)

        await MailAccountState.start_consent.fn(state, 1)  # ty: ignore[unresolved-attribute]

        assert state.busy is False


@pytest.mark.usefixtures("sealed")
@REFUSED
class TestTheImportPanel:
    """Queueing an import is work against somebody's mailbox."""

    async def test_a_refused_caller_gets_no_job_rows(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = ImportJobState()
        _visitors(state, monkeypatch, who)

        await ImportJobState.refresh.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.recent == []

    async def test_a_refused_caller_cannot_queue_an_import(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = ImportJobState()
        state.account_id = 1
        _visitors(state, monkeypatch, who)

        started = await ImportJobState.start_import.fn(state)  # ty: ignore[unresolved-attribute]

        assert started is None
        assert state.job_id == 0
        assert state.polling is False

    async def test_a_refused_caller_cannot_cancel_somebody_elses_import(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = ImportJobState()
        state.job_id = 42
        _visitors(state, monkeypatch, who)

        await ImportJobState.cancel_import.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.cancelling is False

    async def test_a_refused_caller_cannot_drive_the_poll_by_name(
        self, who: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = ImportJobState()
        state.job_id = 42
        state.polling = True
        _visitors(state, monkeypatch, who)

        await ImportJobState.poll.fn(state)  # ty: ignore[unresolved-attribute]

        assert state.polling is False
        assert state.recent == []
