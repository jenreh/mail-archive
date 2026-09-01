"""Tests for :mod:`mailarc_ui.accounts.state`.

Against a real SQLite file and a real :class:`ProviderRegistry`, because both
claims worth proving here are about the seams: that the account form is built
out of whatever a provider declared, and that creating an account writes the
row *and* the secret that opens it.

The second provider in this module is a stub nobody ships. That is the point —
it is registered the way ``app/composition.py`` registers Gmail, and the form
grows its fields without a line of UI changing.
"""

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from appkit_commons.database.configuration import DatabaseConfig
from appkit_commons.database.entities import Base
from appkit_commons.registry import service_registry
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mailarc_core.database.entities import (
    AccountStatus,
    CredentialKind,
    MailAccountEntity,
    MailCredentialEntity,
)
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import (
    AccountIdentity,
    CredentialField,
    EmailAddress,
    MailProvider,
    ProviderDescriptor,
)
from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY, MailSourceFactory
from mailarc_sync.engine import FAKE_DESCRIPTOR, FakeMailSource, ProviderRegistry
from mailarc_sync.erase import AccountBusy, AccountEraser, EraseCounts
from mailarc_ui.accounts.components import (
    account_detail,
    account_settings,
    accounts_list,
    add_account_form,
)
from mailarc_ui.accounts.state import (
    EMAIL_FIELD,
    HALF_A_CREDENTIAL,
    NOT_AN_ADDRESS,
    PICK_A_PROVIDER,
    PROVIDER_FIELD,
    MailAccountState,
    account_eraser,
    provider_registry,
)
from mailarc_ui.imports import ImportJobState
from mailarc_ui.kit import REQUIRED


def _props(node: Any, found: list[str] | None = None) -> list[str]:
    """Every rendered prop in a tree, both branches of a cond walked.

    The same helper ``test_ui_search_form`` uses, for the same reason: a
    ``disabled`` that follows a state var is a prop somewhere in a nested
    render, and reading the source cannot tell a live one from a dead branch.
    """
    found = [] if found is None else found
    if isinstance(node, list):
        for one in node:
            _props(one, found)
        return found
    if not isinstance(node, dict):
        return found
    found.extend(one for one in node.get("props", []) if isinstance(one, str))
    _props(node.get("children", []), found)
    for branch in ("true_value", "false_value"):
        if (subtree := node.get(branch)) is not None:
            _props(subtree, found)
    return found


STATE_MODULE = "mailarc_ui.accounts.state"

TYPED_PASSWORD = "hunter2"  # noqa: S105 - a fixture
"""What the human typed into the form.

Named rather than repeated at each call so that
``test_a_failed_credential_write_reaches_neither_the_log_nor_the_page`` cannot
quietly go vacuous: it asserts this string is absent, which proves nothing if
the form has meanwhile been filled with another one.
"""

IMAP_DESCRIPTOR = ProviderDescriptor(
    provider=MailProvider.IMAP,
    label="IMAP mailbox",
    credential_fields=(
        CredentialField(name="host", label="Server", placeholder="imap.example.com"),
        CredentialField(name="password", label="Password", secret=True),
        CredentialField(name="note", label="Note", required=False),
    ),
)


class StubSource:
    """A mailbox that answers ``verify()`` the way the test told it to."""

    provider = MailProvider.IMAP

    def __init__(self, secret: str, error: Exception | None) -> None:
        self.secret = secret
        self.error = error
        self.closed = False

    async def verify(self) -> AccountIdentity:
        if self.error is not None:
            raise self.error
        return AccountIdentity(
            provider=self.provider,
            address=EmailAddress(address="jens@example.com"),
        )

    async def aclose(self) -> None:
        self.closed = True


class StubFactory:
    """The registered factory, plus a record of what it was handed."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.built: list[StubSource] = []

    def __call__(self, account: object, secret: str) -> StubSource:
        source = StubSource(secret, self.error)
        self.built.append(source)
        return source


class SessionSpy:
    """Stands in for ``get_asyncdb_session`` and watches while it is open.

    Same transaction contract as appkit's: commit when the block leaves
    cleanly, roll back when it does not.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.opened = 0
        self.watching: Callable[[], None] | None = None

    @contextlib.asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        self.opened += 1
        if self.watching is not None:
            self.watching()
        async with self._factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


class StubEraser:
    """Stands in for the eraser the composition root publishes.

    Registered under :class:`AccountEraser` because that is the key the page
    reads, and it is a stand-in rather than a real one for the same reason the
    provider factory here is: what the eraser *does* is proved in
    ``mailarc-sync`` against real stores. What this file proves is the page —
    that the confirmation gates it, that the counts become a sentence, and that
    a mailbox in use is reported as busy rather than as broken.
    """

    def __init__(self) -> None:
        self.counts = EraseCounts(messages=12, archived_rows=12, checkpoints=1)
        self.error: Exception | None = None
        self.cleared: list[int] = []

    async def erase(self, account_id: int, **_: object) -> EraseCounts:
        self.cleared.append(account_id)
        if self.error is not None:
            raise self.error
        return self.counts


@pytest.fixture
def eraser(registered: StubFactory) -> StubEraser:
    """A published eraser, on top of whatever ``registered`` put there."""
    stub = StubEraser()
    service_registry().register_as(AccountEraser, cast(AccountEraser, stub))
    return stub


@pytest.fixture
def registered() -> Iterator[StubFactory]:
    """A registry with two providers, and the encryption key the column needs."""
    factory = StubFactory()
    registry = ProviderRegistry()
    registry.register(FAKE_DESCRIPTOR, FakeMailSource.create)
    registry.register(IMAP_DESCRIPTOR, cast(MailSourceFactory, factory))

    services = service_registry()
    saved = services.snapshot()
    services.register(registry)
    services.register_as(
        DatabaseConfig,
        DatabaseConfig.model_validate(
            {"encryption_key": Fernet.generate_key().decode()}
        ),
    )
    yield factory
    services.restore(saved)


@pytest.fixture
async def sessions(tmp_path) -> AsyncIterator[SessionSpy]:
    """The state's database, on a file of its own."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    spy = SessionSpy(async_sessionmaker(engine, expire_on_commit=False))
    with patch(f"{STATE_MODULE}.get_asyncdb_session", spy):
        yield spy
    await engine.dispose()


@pytest.fixture
def state() -> MailAccountState:
    """The page as it is driven."""
    return MailAccountState()


async def _filled(state: MailAccountState) -> None:
    """A form the human has finished with."""
    state.select_provider(MailProvider.IMAP.value)
    state.set_email_address("jens@example.com")
    state.set_display_name("Work")
    state.set_credential("host", "imap.example.com")
    state.set_credential("password", TYPED_PASSWORD)


async def _accounts(spy: SessionSpy) -> list[MailAccountEntity]:
    async with spy() as session:
        result = await session.execute(select(MailAccountEntity))
        return list(result.scalars().all())


async def _credentials(spy: SessionSpy) -> list[MailCredentialEntity]:
    async with spy() as session:
        result = await session.execute(select(MailCredentialEntity))
        return list(result.scalars().all())


async def _run_consent(state: MailAccountState, account_id: int) -> None:
    """Drive the background handler without Reflex's state lock."""
    with (
        patch.object(MailAccountState, "__aenter__", AsyncMock()),
        patch.object(MailAccountState, "__aexit__", AsyncMock(return_value=False)),
    ):
        await MailAccountState.start_consent.fn(state, account_id)


async def _run_clear(state: MailAccountState) -> None:
    """Drive the clear-out handler without Reflex's state lock."""
    with (
        patch.object(MailAccountState, "__aenter__", AsyncMock()),
        patch.object(MailAccountState, "__aexit__", AsyncMock(return_value=False)),
    ):
        await MailAccountState.clear_account.fn(state)


class TestTheGeneratedForm:
    async def test_offers_every_registered_provider(
        self, state, registered, sessions
    ) -> None:
        await state.load()

        assert state.provider_options == [
            {"value": "fake", "label": FAKE_DESCRIPTOR.label},
            {"value": "imap", "label": "IMAP mailbox"},
        ]
        assert state.error == ""

    async def test_renders_the_fields_the_descriptor_declared(
        self, state, registered
    ) -> None:
        state.select_provider(MailProvider.IMAP.value)

        assert [(one.name, one.label) for one in state.credential_fields] == [
            ("host", "Server"),
            ("password", "Password"),
            ("note", "Note"),
        ]
        # The provider decides which box is masked and which one may stay empty.
        assert [one.secret for one in state.credential_fields] == [False, True, False]
        assert [one.required for one in state.credential_fields] == [True, True, False]
        assert state.credential_fields[0].placeholder == "imap.example.com"

    async def test_a_second_provider_costs_no_ui_line(self, state, registered) -> None:
        """The same code renders a different form for a different descriptor."""
        state.select_provider(MailProvider.FAKE.value)
        folder_fields = [one.name for one in state.credential_fields]

        state.select_provider(MailProvider.IMAP.value)

        assert folder_fields == ["directory"]
        assert [one.name for one in state.credential_fields] == [
            "host",
            "password",
            "note",
        ]

    async def test_clearing_the_provider_clears_the_form(
        self, state, registered
    ) -> None:
        state.select_provider(MailProvider.IMAP.value)

        state.select_provider("")

        assert state.credential_fields == []
        assert state.has_credential_fields is False

    async def test_a_second_load_keeps_the_provider_the_human_picked(
        self, state, registered, sessions
    ) -> None:
        await state.load()
        state.select_provider(MailProvider.IMAP.value)

        await state.load()

        assert state.provider == "imap"

    async def test_a_load_without_a_registry_says_so_instead_of_raising(
        self, state, sessions
    ) -> None:
        services = service_registry()
        saved = services.snapshot()
        services.clear()
        try:
            await state.load()
        finally:
            services.restore(saved)

        assert "app.composition" in state.error
        assert state.busy is False
        assert state.provider_options == []

    async def test_load_preselects_the_first_registered_provider(
        self, state, registered, sessions
    ) -> None:
        await state.load()

        assert state.provider == "fake"
        assert [one.name for one in state.credential_fields] == ["directory"]


class TestCreateAccount:
    async def test_writes_the_account_and_the_credential(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)

        await state.create_account()

        accounts = await _accounts(sessions)
        credentials = await _credentials(sessions)
        assert [(one.provider, one.email_address) for one in accounts] == [
            ("imap", "jens@example.com")
        ]
        assert len(credentials) == 1
        assert credentials[0].account_id == accounts[0].id
        assert state.error == ""

    async def test_the_secret_is_json_under_the_declared_names(
        self, state, registered, sessions
    ) -> None:
        """What goes into the column is the provider's own vocabulary, not ours."""
        await _filled(state)

        await state.create_account()

        credentials = await _credentials(sessions)
        assert json.loads(credentials[0].secret) == {
            "host": "imap.example.com",
            "password": "hunter2",
            "note": "",
        }

    async def test_shows_the_new_account_and_opens_it_for_editing(
        self, state, registered, sessions
    ) -> None:
        """Adding is never the last step, so the form becomes that mailbox's own.

        It used to empty instead. That was right while the page was one column
        of cards and the form was only ever an *add* form; now the same form
        edits the mailbox that is open, and emptying it would leave the new
        mailbox showing blank fields it would then save.
        """
        await _filled(state)

        await state.create_account()

        assert [row.email_address for row in state.accounts] == ["jens@example.com"]
        assert state.accounts[0].display_name == "Work"
        assert state.accounts[0].status == AccountStatus.IDLE
        assert state.has_accounts is True
        assert state.selected_id == state.accounts[0].id
        assert state.email_address == "jens@example.com"
        assert state.display_name == "Work"
        assert state.replacing_credentials is False
        assert state._typed == {}

    async def test_a_missing_required_field_lands_under_that_field(
        self, state, registered, sessions
    ) -> None:
        """Under the box, not over the form — see ``kit.validation``."""
        await _filled(state)
        state.set_credential("password", "  ")

        await state.create_account()

        assert state.errors["password"] == REQUIRED
        assert state.error == ""
        assert await _accounts(sessions) == []

    async def test_an_unpicked_provider_lands_under_the_provider_field(
        self, state, registered, sessions
    ) -> None:
        state.set_email_address("jens@example.com")

        await state.create_account()

        assert state.errors[PROVIDER_FIELD] == PICK_A_PROVIDER
        assert await _accounts(sessions) == []

    async def test_a_missing_address_lands_under_the_address_field(
        self, state, registered, sessions
    ) -> None:
        state.select_provider(MailProvider.IMAP.value)
        state.set_credential("host", "imap.example.com")
        state.set_credential("password", TYPED_PASSWORD)

        await state.create_account()

        assert state.errors[EMAIL_FIELD] == REQUIRED
        assert await _accounts(sessions) == []

    async def test_the_database_saying_no_lands_in_error_too(
        self, state, registered, sessions
    ) -> None:
        """The same address twice violates the natural key — and must not raise."""
        await _filled(state)
        await state.create_account()

        await _filled(state)
        await state.create_account()

        assert "UNIQUE" in state.error.upper()
        assert len(await _accounts(sessions)) == 1
        assert state.busy is False

    async def test_a_failed_credential_write_reaches_neither_the_log_nor_the_page(
        self, state, registered, sessions, caplog
    ) -> None:
        """The handler that reports the failure must not be the one that leaks it.

        This is the ``EncryptedString`` hole end to end. ``_create`` writes the
        typed secret into a column whose bind processing does the encrypting,
        so a misconfigured ``app_database_encryption_key`` fails the write —
        and a raw ``StatementError`` says so while quoting its bind parameters,
        which for that statement is the plaintext. ``create_account`` then
        hands it to ``logger.exception`` *and* to ``error``, a state var that
        is rendered in the browser, so one bad setting would put a password
        into the application log and onto the page at once.

        The guard is in :meth:`MailCredentialRepository.store_secret`; this
        asserts it holds as far as both of those outputs, and that what is left
        still says why the write failed.
        """
        await _filled(state)
        service_registry().register_as(
            DatabaseConfig,
            DatabaseConfig.model_validate({"encryption_key": "not-a-fernet-key"}),
        )

        with caplog.at_level(logging.ERROR):
            await state.create_account()

        assert TYPED_PASSWORD not in caplog.text
        assert TYPED_PASSWORD not in state.error
        assert "Fernet" in caplog.text, "the reason has to survive the stripping"
        assert "Fernet" in state.error
        assert state.busy is False

    async def test_the_busy_flag_goes_up_and_down(
        self, state, registered, sessions
    ) -> None:
        seen: list[bool] = []
        sessions.watching = lambda: seen.append(state.busy)
        await _filled(state)

        await state.create_account()

        assert seen, "the write never opened a session"
        assert all(seen), "busy has to be set while the write is in flight"
        assert state.busy is False


class TestDeleteAccount:
    async def test_takes_the_credential_with_it(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()

        await state.delete_account(state.accounts[0].id)

        assert await _accounts(sessions) == []
        assert await _credentials(sessions) == []
        assert state.accounts == []
        assert state.busy is False

    async def test_a_database_failure_lands_in_error(
        self, state, registered, sessions
    ) -> None:
        with patch.object(
            SessionSpy, "__call__", side_effect=RuntimeError("database is locked")
        ):
            await state.delete_account(404)

        assert state.error == "database is locked"
        assert state.busy is False


class TestConsent:
    async def test_drives_the_provider_through_the_port(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()

        await _run_consent(state, state.accounts[0].id)

        source = registered.built[0]
        assert json.loads(source.secret)["host"] == "imap.example.com"
        assert source.closed is True
        assert state.error == ""
        assert state.busy is False

    async def test_records_that_the_mailbox_answered(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()
        account_id = state.accounts[0].id
        async with sessions() as session:
            (
                await session.get(MailAccountEntity, account_id)
            ).status = AccountStatus.AUTH_ERROR

        await _run_consent(state, account_id)

        assert state.accounts[0].status == AccountStatus.IDLE
        assert state.accounts[0].last_error == ""

    async def test_a_rejected_credential_is_kept_and_shown(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()
        registered.error = MailAuthError("the refresh token was revoked")

        await _run_consent(state, state.accounts[0].id)

        assert state.error == "the refresh token was revoked"
        assert state.busy is False
        stored = await _accounts(sessions)
        assert stored[0].status == AccountStatus.AUTH_ERROR
        assert stored[0].last_error == "the refresh token was revoked"
        assert registered.built[0].closed is True

    async def test_an_account_without_a_credential_says_so(
        self, state, registered, sessions
    ) -> None:
        async with sessions() as session:
            account = MailAccountEntity(
                provider="imap",
                display_name="Orphan",
                email_address="orphan@example.com",
            )
            session.add(account)
            await session.flush()
            account_id = account.id

        await _run_consent(state, account_id)

        assert "no stored credential" in state.error

    async def test_an_account_that_is_gone_says_so(
        self, state, registered, sessions
    ) -> None:
        await _run_consent(state, 404)

        assert state.error == "account 404 is gone"
        assert state.busy is False


class TestTheRegistrySeam:
    def test_says_what_is_missing_when_nobody_registered_anything(self) -> None:
        services = service_registry()
        saved = services.snapshot()
        services.clear()
        try:
            with pytest.raises(RuntimeError, match=r"app\.composition"):
                provider_registry()
        finally:
            services.restore(saved)


class TestSelection:
    """The detail column shows one mailbox, and the id is what says which."""

    async def test_nothing_is_selected_before_a_click(self, state) -> None:
        assert state.has_selection is False
        assert state.selected.id == 0
        assert state.selected.email_address == ""

    async def test_selecting_a_mailbox_opens_it(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()
        opened = state.accounts[0]

        state.select(opened.id)

        assert state.has_selection is True
        assert state.selected.email_address == "jens@example.com"

    async def test_the_new_mailbox_is_the_open_one(
        self, state, registered, sessions
    ) -> None:
        """Adding is never the last step — connecting and importing follow."""
        await _filled(state)

        await state.create_account()

        assert state.selected_id == state.accounts[0].id
        assert state.selected.display_name == "Work"

    async def test_the_reading_follows_the_account_not_a_copy(
        self, state, registered, sessions
    ) -> None:
        """`selected` is derived, so a status change reaches the open mailbox."""
        await _filled(state)
        await state.create_account()
        account_id = state.selected_id
        registered.error = MailAuthError("the mailbox refused us")

        await _run_consent(state, account_id)
        await state.load()

        assert state.selected.status == AccountStatus.AUTH_ERROR
        assert state.selected.status_color == "red"
        assert "refused" in state.selected.last_error

    async def test_asking_for_a_new_one_clears_the_selection(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()

        state.start_new()

        assert state.has_selection is False
        assert state.email_address == ""
        assert state.display_name == ""

    async def test_deleting_the_open_mailbox_puts_the_form_back(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()
        account_id = state.selected_id

        await state.delete_account(account_id)

        assert state.has_selection is False
        assert state.accounts == []

    async def test_deleting_another_mailbox_leaves_the_open_one_open(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        await state.create_account()
        kept = state.selected_id
        await _filled(state)
        state.set_email_address("second@example.com")
        await state.create_account()
        dropped = state.selected_id
        state.select(kept)

        await state.delete_account(dropped)

        assert state.selected_id == kept
        assert state.selected.email_address == "jens@example.com"

    async def test_the_count_is_spelled_for_one_and_for_many(
        self, state, registered, sessions
    ) -> None:
        assert state.count_label == "0 mailboxes"
        await _filled(state)
        await state.create_account()

        assert state.count_label == "1 mailbox"


class TestClearingAMailbox:
    """The page's half of the clear-out: a gate, a sentence, and a refusal.

    Clearing is the one action here that destroys mail, and it sits next to
    Delete, which looks the same and means something else. So what is tested is
    mostly the difference between them.
    """

    async def _open_mailbox(self, state, sessions) -> int:
        await _filled(state)
        await state.create_account()
        return state.selected_id

    async def test_the_button_only_opens_the_confirmation(
        self, state, registered, eraser, sessions
    ) -> None:
        """Nothing is deleted by the click that asks."""
        await self._open_mailbox(state, sessions)

        state.ask_clear()

        assert state.confirming_clear is True
        assert eraser.cleared == []

    async def test_cancelling_deletes_nothing(
        self, state, registered, eraser, sessions
    ) -> None:
        await self._open_mailbox(state, sessions)
        state.ask_clear()

        state.cancel_clear()

        assert state.confirming_clear is False
        assert eraser.cleared == []

    async def test_confirming_clears_the_open_mailbox(
        self, state, registered, eraser, sessions
    ) -> None:
        account_id = await self._open_mailbox(state, sessions)
        state.ask_clear()

        await _run_clear(state)

        assert eraser.cleared == [account_id]
        assert state.confirming_clear is False
        assert state.clearing is False

    async def test_the_mailbox_is_still_there_afterwards(
        self, state, registered, eraser, sessions
    ) -> None:
        """The difference from Delete, in one assertion."""
        account_id = await self._open_mailbox(state, sessions)

        await _run_clear(state)

        assert [row.id for row in state.accounts] == [account_id]
        assert state.selected_id == account_id
        assert len(await _accounts(sessions)) == 1

    async def test_the_count_becomes_the_confirmation(
        self, state, registered, eraser, sessions
    ) -> None:
        """A clear-out has nothing else to show for itself — the list is
        unchanged and the mailbox looks exactly as it did."""
        await self._open_mailbox(state, sessions)

        await _run_clear(state)

        assert state.cleared == "Cleared 12 messages."
        assert state.error == ""

    async def test_a_shared_copy_is_mentioned_only_when_there_is_one(
        self, state, registered, eraser, sessions
    ) -> None:
        """Mail two mailboxes hold stays in the archive, and that is surprising."""
        await self._open_mailbox(state, sessions)
        eraser.counts = EraseCounts(messages=3, copies=2)

        await _run_clear(state)

        assert state.cleared == (
            "Cleared 3 messages. 2 messages stay in the archive under another mailbox."
        )

    async def test_a_mailbox_with_nothing_imported_says_so(
        self, state, registered, eraser, sessions
    ) -> None:
        await self._open_mailbox(state, sessions)
        eraser.counts = EraseCounts()

        await _run_clear(state)

        assert state.cleared == "There was nothing imported to clear."

    async def test_one_message_is_not_spelled_as_several(
        self, state, registered, eraser, sessions
    ) -> None:
        await self._open_mailbox(state, sessions)
        eraser.counts = EraseCounts(messages=1, copies=1)

        await _run_clear(state)

        assert state.cleared == (
            "Cleared 1 message. 1 message stays in the archive under another mailbox."
        )

    async def test_a_busy_mailbox_is_reported_as_busy(
        self, state, registered, eraser, sessions
    ) -> None:
        """The one failure a person fixes by waiting."""
        await self._open_mailbox(state, sessions)
        eraser.error = AccountBusy("An import is still running for this mailbox")

        await _run_clear(state)

        assert state.error == "An import is still running for this mailbox"
        assert state.cleared == ""
        assert state.clearing is False

    async def test_a_failure_lands_in_error_and_releases_the_page(
        self, state, registered, eraser, sessions
    ) -> None:
        await self._open_mailbox(state, sessions)
        eraser.error = ConnectionError("the graph is not answering")

        await _run_clear(state)

        assert state.error == "the graph is not answering"
        assert state.clearing is False

    async def test_confirming_with_nothing_open_does_nothing(
        self, state, registered, eraser, sessions
    ) -> None:
        """There is no mailbox to name, so there is nothing to clear."""
        await _run_clear(state)

        assert eraser.cleared == []
        assert state.clearing is False

    async def test_opening_another_mailbox_drops_the_last_notice(
        self, state, registered, eraser, sessions
    ) -> None:
        """A count belongs to the mailbox it was counted for."""
        await self._open_mailbox(state, sessions)
        await _run_clear(state)

        state.select(0)

        assert state.cleared == ""

    async def test_the_dialog_can_close_itself_but_never_open_itself(
        self, state, registered, eraser, sessions
    ) -> None:
        """``on_open_change`` reports both directions; only one is obeyed.

        A handler that could open a destructive confirmation from a stray
        event would put it on screen without anybody asking for it.
        """
        await self._open_mailbox(state, sessions)

        state.set_confirming_clear(True)
        assert state.confirming_clear is False

        state.ask_clear()
        state.set_confirming_clear(False)
        assert state.confirming_clear is False


class TestTheEraserSeam:
    def test_says_what_is_missing_when_nobody_registered_one(self) -> None:
        services = service_registry()
        saved = services.snapshot()
        services.clear()
        try:
            with pytest.raises(RuntimeError, match=r"app\.composition"):
                account_eraser()
        finally:
            services.restore(saved)


class TestEditingAMailbox:
    """The detail column writes back: name, address, and a retyped secret."""

    @staticmethod
    async def _added(state: MailAccountState) -> int:
        await _filled(state)
        await state.create_account()
        return state.selected_id

    async def test_opening_a_mailbox_fills_the_form_with_its_values(
        self, state, registered, sessions
    ) -> None:
        account_id = await self._added(state)
        state.start_new()

        state.select(account_id)

        assert state.email_address == "jens@example.com"
        assert state.display_name == "Work"
        assert state.provider == MailProvider.IMAP.value
        assert [field.name for field in state.credential_fields] == [
            "host",
            "password",
            "note",
        ]

    async def test_opening_a_mailbox_closes_the_credential_fields(
        self, state, registered, sessions
    ) -> None:
        """A stored secret is never shown, so replacing one is asked for."""
        account_id = await self._added(state)
        state.replace_credentials()

        state.select(account_id)

        assert state.replacing_credentials is False

    async def test_saving_writes_the_new_name_and_address(
        self, state, registered, sessions
    ) -> None:
        await self._added(state)
        state.set_display_name("Work archive")
        state.set_email_address("jens.rehpoehler@example.com")

        await state.save_account()

        accounts = await _accounts(sessions)
        assert accounts[0].display_name == "Work archive"
        assert accounts[0].email_address == "jens.rehpoehler@example.com"
        assert state.selected.email_address == "jens.rehpoehler@example.com"
        assert state.error == ""

    async def test_saving_without_retyping_keeps_the_stored_secret(
        self, state, registered, sessions
    ) -> None:
        """The form cannot read a secret back, so an untouched box changes none."""
        await self._added(state)
        before = (await _credentials(sessions))[0].secret
        state.set_display_name("Renamed")

        await state.save_account()

        credentials = await _credentials(sessions)
        assert len(credentials) == 1
        assert credentials[0].secret == before

    async def test_retyping_replaces_the_secret_in_place(
        self, state, registered, sessions
    ) -> None:
        await self._added(state)
        state.replace_credentials()
        state.set_credential("host", "imap.other.example")
        state.set_credential("password", "new-app-password")

        await state.save_account()

        credentials = await _credentials(sessions)
        assert len(credentials) == 1
        assert json.loads(credentials[0].secret) == {
            "host": "imap.other.example",
            "password": "new-app-password",
            "note": "",
        }

    async def test_half_a_retyped_credential_is_refused(
        self, state, registered, sessions
    ) -> None:
        """Typing into one field commits to all of them: half a secret opens nothing."""
        await self._added(state)
        before = (await _credentials(sessions))[0].secret
        state.replace_credentials()
        state.set_credential("host", "imap.other.example")

        await state.save_account()

        assert state.errors["password"] == HALF_A_CREDENTIAL
        assert (await _credentials(sessions))[0].secret == before

    async def test_an_empty_address_is_refused(
        self, state, registered, sessions
    ) -> None:
        await self._added(state)
        state.set_email_address("   ")

        await state.save_account()

        assert state.errors[EMAIL_FIELD] == REQUIRED
        assert (await _accounts(sessions))[0].email_address == "jens@example.com"

    async def test_a_mailbox_without_a_name_is_called_by_its_address(
        self, state, registered, sessions
    ) -> None:
        await self._added(state)
        state.set_display_name("  ")

        await state.save_account()

        assert (await _accounts(sessions))[0].display_name == "jens@example.com"

    async def test_saving_with_nothing_open_says_so(
        self, state, registered, sessions
    ) -> None:
        state.set_email_address("jens@example.com")

        await state.save_account()

        assert "No mailbox is open" in state.error

    async def test_a_provider_this_process_never_registered_still_opens(
        self, state, registered, sessions
    ) -> None:
        """The list shows such a mailbox, so the form beside it must render."""
        async with sessions() as session:
            account = MailAccountEntity(
                provider="something-else",
                display_name="Imported elsewhere",
                email_address="old@example.com",
            )
            session.add(account)
            await session.flush()
            account_id = account.id
        await state.load()

        state.select(account_id)

        assert state.credential_fields == []
        assert state.email_address == "old@example.com"


class TestComponents:
    def test_the_list_compiles(self) -> None:
        """Rendering is the only way to catch a prop appkit_mantine lacks."""
        rendered = str(accounts_list().render())

        assert "mail_account_state.select" in rendered
        assert "mail_account_state.start_new" in rendered

    def test_a_click_carries_the_pages_own_handler_too(self) -> None:
        """The import panel is pointed at the mailbox by the same click.

        Both handlers on one click is the whole seam: without the second, the
        import panel keeps acting on whatever was selected before.
        """
        rendered = str(accounts_list(on_select=ImportJobState.select_account).render())

        assert "mail_account_state.select" in rendered
        assert "import_job_state.select_account" in rendered

    def test_the_form_compiles(self) -> None:
        """The generated form is a `rx.foreach`; a broken binding fails here."""
        rendered = str(add_account_form().render())

        assert "set_credential" in rendered

    def test_the_detail_column_offers_all_three_actions(self) -> None:
        rendered = str(account_detail().render())

        assert "start_consent" in rendered
        assert "ask_clear" in rendered
        assert "delete_account" in rendered

    def test_the_detail_column_edits_the_open_mailbox(self) -> None:
        """The form writes back, and the credential boxes have to be asked for."""
        rendered = str(account_settings().render())

        assert "save_account" in rendered
        assert "replace_credentials" in rendered
        assert "set_email_address" in rendered
        assert "set_display_name" in rendered

    def test_clearing_goes_through_a_confirmation(self) -> None:
        """The button asks; only the dialog's action clears.

        Rendering is the only way to catch this: a ``Clear`` wired straight to
        ``clear_account`` would look identical in the source of the page.
        """
        rendered = str(account_detail().render())

        assert "clear_account" in rendered
        assert "cancel_clear" in rendered
        assert "Clear this mailbox?" in rendered


class TestTheConsentStep:
    """The half that was missing: a provider whose registration carries one.

    Until this was wired, `start_consent` only ever called `verify()`, and a
    Gmail account — whose stored form values are not yet a usable credential —
    could not be connected at all. The page still names no provider: it asks
    the registry whether this one has a second step.
    """

    @staticmethod
    def _with_consent(granted: str, calls: list[dict[str, str]]) -> None:
        """Re-register IMAP so it now needs a browser round trip."""

        async def run(values):
            calls.append(dict(values))
            return granted

        registry = provider_registry()
        registry.register(
            IMAP_DESCRIPTOR, registry.factory_for(MailProvider.IMAP), consent=run
        )

    async def test_the_runner_gets_what_the_user_typed(
        self, state, registered, sessions
    ) -> None:
        calls: list[dict[str, str]] = []
        self._with_consent(json.dumps({"granted": True}), calls)
        await _filled(state)
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)

        # Every field the descriptor declared, optional ones included and empty
        # — the runner is the provider's own code and decides what it needs —
        # plus the account's address, so a browser-based runner can say which
        # account the identity provider should open on.
        assert calls == [
            {
                "host": "imap.example.com",
                "password": "hunter2",
                "note": "",
                CONSENT_ADDRESS_KEY: "jens@example.com",
            }
        ]

    async def test_what_it_returns_replaces_the_stored_secret(
        self, state, registered, sessions
    ) -> None:
        granted = json.dumps({"host": "imap.example.com", "token": "earned"})
        self._with_consent(granted, [])
        await _filled(state)
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)

        rows = await _credentials(sessions)
        assert [row.secret for row in rows] == [granted]
        assert [row.kind for row in rows] == [CredentialKind.OAUTH], (
            "the kind every reader actually looks under"
        )

    async def test_the_granted_secret_is_the_one_the_source_is_built_with(
        self, state, registered, sessions
    ) -> None:
        """A consent whose result nobody uses is a consent that did nothing."""
        granted = json.dumps({"token": "earned"})
        self._with_consent(granted, [])
        await _filled(state)
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)

        assert registered.built[-1].secret == granted

    async def test_a_second_consent_reuses_what_the_first_was_given(
        self, state, registered, sessions
    ) -> None:
        """The granted blob spells the same field names, so nobody retypes them."""
        calls: list[dict[str, str]] = []
        self._with_consent(
            json.dumps({"host": "imap.example.com", "token": "a"}), calls
        )
        await _filled(state)
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)
        await _run_consent(state, account_id)

        assert calls[1] == {
            "host": "imap.example.com",
            "token": "a",
            CONSENT_ADDRESS_KEY: "jens@example.com",
        }

    async def test_the_account_ends_up_idle_and_without_an_error(
        self, state, registered, sessions
    ) -> None:
        self._with_consent(json.dumps({"token": "earned"}), [])
        await _filled(state)
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)

        account = (await _accounts(sessions))[0]
        assert account.status == AccountStatus.IDLE
        assert account.last_error is None
        assert state.error == ""

    async def test_a_refused_consent_lands_in_auth_error(
        self, state, registered, sessions
    ) -> None:
        async def refuse(values):
            raise MailAuthError("the user closed the tab")

        registry = provider_registry()
        registry.register(
            IMAP_DESCRIPTOR,
            registry.factory_for(MailProvider.IMAP),
            consent=refuse,
        )
        await _filled(state)
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)

        account = (await _accounts(sessions))[0]
        assert account.status == AccountStatus.AUTH_ERROR
        assert "closed the tab" in (account.last_error or "")
        assert registered.built == [], (
            "nothing was built from a credential we never got"
        )

    async def test_a_provider_without_a_consent_step_still_just_verifies(
        self, state, registered, sessions
    ) -> None:
        """IMAP as registered by the fixture: typed is complete, no browser."""
        await _filled(state)
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)

        rows = await _credentials(sessions)
        assert [row.kind for row in rows] == [CredentialKind.PASSWORD]
        assert (await _accounts(sessions))[0].status == AccountStatus.IDLE


class TestAProviderThatAsksForNothing:
    """Gmail, now that its OAuth client is the installation's.

    The form still generates itself from the descriptor — declaring no fields
    renders no fields — and there is nothing to write down until consent has
    earned something.
    """

    @staticmethod
    def _silent() -> ProviderDescriptor:
        return ProviderDescriptor(
            provider=MailProvider.GMAIL, label="Gmail", credential_fields=()
        )

    def _register(self, consent=None) -> None:
        registry = provider_registry()
        registry.register(
            self._silent(),
            cast(MailSourceFactory, lambda account, secret: StubSource(secret, None)),
            consent=consent,
        )

    async def test_the_form_shows_no_credential_boxes(self, state, registered) -> None:
        self._register()

        state.select_provider(MailProvider.GMAIL.value)

        assert state.credential_fields == []
        assert state.has_credential_fields is False

    async def test_creating_the_account_writes_no_credential_row(
        self, state, registered, sessions
    ) -> None:
        """An empty `{}` in an encrypted column is something a later reader
        would mistake for a credential."""
        self._register()
        state.select_provider(MailProvider.GMAIL.value)
        state.set_email_address("jens@example.com")

        await state.create_account()

        assert len(await _accounts(sessions)) == 1
        assert await _credentials(sessions) == []
        assert state.error == ""

    async def test_the_consent_is_what_creates_the_credential(
        self, state, registered, sessions
    ) -> None:
        granted = json.dumps({"refresh_token": "1//earned"})

        async def run(values):
            # Nothing was typed, so nothing typed is passed — only the address
            # the runner needs to open Google's consent on the right account.
            assert values == {CONSENT_ADDRESS_KEY: "jens@example.com"}
            return granted

        self._register(consent=run)
        state.select_provider(MailProvider.GMAIL.value)
        state.set_email_address("jens@example.com")
        await state.create_account()
        account_id = (await _accounts(sessions))[0].id

        await _run_consent(state, account_id)

        rows = await _credentials(sessions)
        assert [(row.kind, row.secret) for row in rows] == [
            (CredentialKind.OAUTH, granted)
        ]
        assert (await _accounts(sessions))[0].status == AccountStatus.IDLE


class TestTheIdentityCheck:
    """The mailbox says whose it is; the row says whose it should be.

    A consent screen lets a person signed in to several accounts pick any of
    them, and a Gmail refresh token for the wrong one would sync the wrong
    mailbox into this account's archive — silently, forever.
    """

    async def _account_for(self, state, sessions, address: str) -> int:
        state.select_provider(MailProvider.IMAP.value)
        state.set_email_address(address)
        state.set_credential("host", "imap.example.com")
        state.set_credential("password", TYPED_PASSWORD)
        await state.create_account()
        return (await _accounts(sessions))[0].id

    async def test_the_address_the_mailbox_reports_must_be_the_rows(
        self, state, registered, sessions
    ) -> None:
        account_id = await self._account_for(state, sessions, "other@example.com")

        await _run_consent(state, account_id)

        account = (await _accounts(sessions))[0]
        assert account.status == AccountStatus.AUTH_ERROR
        assert account.last_error is not None
        assert "jens@example.com" in account.last_error
        assert "other@example.com" in account.last_error
        assert state.error == account.last_error

    async def test_a_wrong_accounts_credential_is_not_kept(
        self, state, registered, sessions
    ) -> None:
        """Whatever was granted opens somebody else's mailbox; it goes."""
        registry = provider_registry()

        async def run(values):
            return json.dumps({"token": "for-the-wrong-account"})

        registry.register(
            IMAP_DESCRIPTOR, registry.factory_for(MailProvider.IMAP), consent=run
        )
        account_id = await self._account_for(state, sessions, "other@example.com")

        await _run_consent(state, account_id)

        assert await _credentials(sessions) == []

    async def test_case_and_whitespace_do_not_make_it_a_different_mailbox(
        self, state, registered, sessions
    ) -> None:
        account_id = await self._account_for(state, sessions, " Jens@Example.com ")

        await _run_consent(state, account_id)

        assert (await _accounts(sessions))[0].status == AccountStatus.IDLE
        assert state.error == ""


class TestWhatMakesTheFormValid:
    """The rules, checked where the values are — the shape is ``kit.validation``.

    Every one of these is a message that appears while somebody types, which
    is the reason the complaint is held in state rather than raised at save
    time: a form that stays silent until Save and then reports four things at
    once is a form people submit twice.
    """

    async def test_an_address_with_no_at_sign_is_refused(self, state) -> None:
        state.set_email_address("jens.example.com")

        assert state.errors[EMAIL_FIELD] == NOT_AN_ADDRESS
        assert state.has_errors is True

    async def test_an_address_with_nothing_before_the_at_sign_is_refused(
        self, state
    ) -> None:
        state.set_email_address("@example.com")

        assert state.errors[EMAIL_FIELD] == NOT_AN_ADDRESS

    async def test_a_plausible_address_is_left_alone(self, state) -> None:
        """Deliberately shallow — the archive is not an address validator, and
        anything stricter would reject addresses that exist."""
        state.set_email_address("jens+archive@sub.example.co.uk")

        assert state.errors == {}
        assert state.has_errors is False

    async def test_fixing_an_address_takes_the_complaint_back(self, state) -> None:
        state.set_email_address("nope")

        state.set_email_address("jens@example.com")

        assert state.errors == {}

    async def test_a_name_has_no_rule(self, state) -> None:
        """A mailbox with no name is called by its address."""
        state.set_email_address("jens@example.com")
        state.set_display_name("")

        assert state.errors == {}

    async def test_typing_one_credential_makes_the_others_required(
        self, state, registered
    ) -> None:
        """Half a secret opens nothing, so touching one box commits to all."""
        state.select_provider(MailProvider.IMAP.value)

        state.set_credential("host", "imap.example.com")

        assert state.errors["password"] == HALF_A_CREDENTIAL

    async def test_untouched_credential_boxes_are_not_required(
        self, state, registered
    ) -> None:
        """An empty box means *keep what is stored* — see ``_update``."""
        state.select_provider(MailProvider.IMAP.value)

        assert state.errors == {}
        assert state.has_errors is False

    async def test_filling_the_last_box_clears_the_first_ones_complaint(
        self, state, registered
    ) -> None:
        state.select_provider(MailProvider.IMAP.value)
        state.set_credential("host", "imap.example.com")

        state.set_credential("password", TYPED_PASSWORD)

        assert state.errors == {}

    async def test_opening_another_mailbox_forgets_the_complaints(
        self, state, registered, sessions
    ) -> None:
        """Otherwise the message about the mailbox somebody was looking at a
        moment ago stays on screen against the values of the next one."""
        await _filled(state)
        await state.create_account()
        state.set_email_address("nope")

        state.select(state.selected_id)

        assert state.errors == {}

    async def test_choosing_a_provider_again_forgets_them_too(
        self, state, registered
    ) -> None:
        """The complaints belonged to fields the descriptor has just rebuilt,
        so they are about boxes that are not on the page any more."""
        state.select_provider(MailProvider.IMAP.value)
        state.set_credential("host", "imap.example.com")
        assert state.has_errors is True

        state.select_provider(MailProvider.IMAP.value)

        assert state.errors == {}

    async def test_a_form_wrong_in_two_places_says_both(
        self, state, registered, sessions
    ) -> None:
        """Every rule runs rather than the first failing one: a person who
        fixes one thing and is then told about the next has been made to
        submit twice to learn what was always on screen."""
        state.select_provider(MailProvider.IMAP.value)

        await state.create_account()

        assert state.errors[EMAIL_FIELD] == REQUIRED
        assert state.errors["host"] == REQUIRED
        assert await _accounts(sessions) == []


class TestTheWriteButtonsStayPressable:
    """A press is how a person asks what is still wrong.

    Read off the render, because the bug this covers is invisible to a state
    test: ``create_account`` reports every problem at once when it is *called*,
    and the button was disabled the moment the first one appeared — so the form
    said "the address is required", went dead, and never mentioned the empty
    credential beside it. Every state test still passed, because a test calls
    the handler directly and never meets the button.

    Not every field here complains as it is typed into. A credential box the
    provider declared is only checked once somebody touches it, so an untouched
    required one has nothing under it until a press asks.
    """

    def test_neither_button_goes_dead_on_the_first_complaint(self) -> None:
        for form in (add_account_form, account_settings):
            disabled = [
                prop
                for prop in _props(form().render())
                if prop.startswith("disabled:") and "has_errors" in prop
            ]

            assert disabled == [], (
                f"{form.__name__} disables its write button on has_errors, "
                "which is how the second complaint never gets reported"
            )

    async def test_pressing_twice_still_writes_nothing(
        self, state, registered, sessions
    ) -> None:
        """The other half: a pressable button is only safe because pressing it
        writes nothing while the form is wrong."""
        state.select_provider(MailProvider.IMAP.value)

        await state.create_account()
        await state.create_account()

        assert await _accounts(sessions) == []
        assert state.errors[EMAIL_FIELD] == REQUIRED
        assert state.errors["host"] == REQUIRED
