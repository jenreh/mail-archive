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
from collections.abc import AsyncIterator, Callable, Iterator
from typing import cast
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
from mailarc_ui.accounts.components import accounts_panel
from mailarc_ui.accounts.state import MailAccountState, provider_registry

STATE_MODULE = "mailarc_ui.accounts.state"

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
        DatabaseConfig.model_validate({
            "encryption_key": Fernet.generate_key().decode()
        }),
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
    return MailAccountState()


async def _filled(state: MailAccountState) -> None:
    """A form the human has finished with."""
    state.select_provider(MailProvider.IMAP.value)
    state.set_email_address("jens@example.com")
    state.set_display_name("Work")
    state.set_credential("host", "imap.example.com")
    state.set_credential("password", "hunter2")


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
        await MailAccountState.start_consent.fn(  # ty: ignore[unresolved-attribute]
            state, account_id
        )


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

    async def test_shows_the_new_account_and_empties_the_form(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)

        await state.create_account()

        assert [row.email_address for row in state.accounts] == ["jens@example.com"]
        assert state.accounts[0].display_name == "Work"
        assert state.accounts[0].status == AccountStatus.IDLE
        assert state.has_accounts is True
        assert state.email_address == ""
        assert state._typed == {}

    async def test_a_missing_required_field_lands_in_error(
        self, state, registered, sessions
    ) -> None:
        await _filled(state)
        state.set_credential("password", "  ")

        await state.create_account()

        assert state.error == "Password is required."
        assert await _accounts(sessions) == []

    async def test_an_unpicked_provider_lands_in_error(
        self, state, registered, sessions
    ) -> None:
        state.set_email_address("jens@example.com")

        await state.create_account()

        assert state.error == "Pick a provider first."

    async def test_a_missing_address_lands_in_error(
        self, state, registered, sessions
    ) -> None:
        state.select_provider(MailProvider.IMAP.value)
        state.set_credential("host", "imap.example.com")
        state.set_credential("password", "hunter2")

        await state.create_account()

        assert state.error == "An email address is required."

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


class TestComponents:
    def test_the_panel_compiles(self) -> None:
        """The generated form is a `rx.foreach`; a broken binding fails here."""
        rendered = str(accounts_panel().render())

        assert "set_credential" in rendered
        assert "start_consent" in rendered


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
        state.set_credential("password", "hunter2")
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
