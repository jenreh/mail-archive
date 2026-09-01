"""Mail accounts, driven from a page: list them, add one, connect it, drop it.

Phase 4 is deliberately ugly (§10) and this is its account half. The one thing
here that is not throwaway is where the form comes from: what a provider needs
is *declared* on its :class:`~mailarc_core.mail.model.ProviderDescriptor`, so
teaching this page IMAP costs a registration line in ``app/composition.py`` and
no line here.

Two boundaries this module keeps.

``mailarc-ui`` is a component and may not import ``app``, so the provider list
is read out of the service registry — the same route every configuration
takes, and where the composition root leaves its decisions for everything
below it. Never at import time: a registry read at module level would run
before the application filled it.

A SQLAlchemy row never becomes a state var. Reflex has to serialise what a
state holds, and a row whose session has closed hands back nothing, so every
row is projected onto a frozen pydantic model on the way out of the session.
"""

import json
import logging

import reflex as rx
from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from mailarc_core.database.entities import (
    AccountStatus,
    CredentialKind,
    MailAccountEntity,
    MailCredentialEntity,
)
from mailarc_core.database.repositories import (
    MailAccountRepository,
    MailCredentialRepository,
)
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import AccountIdentity, MailProvider
from mailarc_core.mail.ports import CONSENT_ADDRESS_KEY, MailSourcePort
from mailarc_sync.engine import ProviderRegistry
from mailarc_sync.erase import AccountBusy, AccountEraser, EraseCounts
from mailarc_ui.kit import REQUIRED, FieldErrors

logger = logging.getLogger(__name__)

_ACCOUNTS = MailAccountRepository()
_CREDENTIALS = MailCredentialRepository()

FORM_CREDENTIAL_KIND = CredentialKind.PASSWORD
"""The kind this form writes.

What a human typed is stored as they typed it; a token that a consent round
trip hands back belongs to the adapter and lands under ``oauth``. Nothing here
needs to know which of the two a provider ends up with — a reader takes the
first credential an account has, the way ``app/worker.py`` does.
"""


EMAIL_FIELD = "email_address"
PROVIDER_FIELD = "provider"
"""The two fields this repository spells out; the rest a provider declares.

Named constants because each is written in three places — the rule, the
component that reads the message, and the test — and a typo in any one of them
is a field that silently never complains.
"""

NOT_AN_ADDRESS = "That does not look like an email address."
"""Deliberately shallow. The archive is not an address validator and the only
thing it can honestly catch here is a value with no ``@`` in it — anything
stricter would reject addresses that exist."""

PICK_A_PROVIDER = "Pick a provider."

HALF_A_CREDENTIAL = "Fill every credential field, or leave them all empty."
"""What an update says when one box was typed into and another was left blank.

A credential is stored as one JSON value, so a half-filled form does not write
half a credential — it writes a whole one that opens nothing. Adding a mailbox
says :data:`~mailarc_ui.kit.REQUIRED` instead, because there is no stored
secret to keep and empty means empty.
"""


_STATUS_COLORS = {
    AccountStatus.IDLE: "gray",
    AccountStatus.SYNCING: "blue",
    AccountStatus.AUTH_ERROR: "red",
    AccountStatus.ERROR: "red",
}
"""What each status should look like, decided where the other labels are.

A component would have to match on a `Var` to pick this, which is the argument
:mod:`mailarc_ui.imports.state` already makes for its job states. An account
whose status this process does not recognise gets the neutral one.
"""


class AccountRow(BaseModel):
    """One mailbox in the list, and everything the detail column shows of it.

    ``provider`` stays the short name the row carries rather than the
    descriptor's label: a mailbox whose provider this process did not register
    still has to be visible, if only to be deleted.
    """

    model_config = ConfigDict(frozen=True)

    id: int = 0
    provider: str = ""
    display_name: str = ""
    email_address: str = ""
    status: str = ""
    status_color: str = "gray"
    enabled: bool = False
    last_error: str = ""


_NO_ACCOUNT = AccountRow()
"""Nothing selected. A sentinel row keeps every component free of ``None``.

The same device :class:`~mailarc_ui.imports.state.ImportJobRow` uses for the
job nobody has started: a component reads ``selected.email_address`` whether
or not there is a selection, and the page decides with ``has_selection``
whether to draw the detail column at all.
"""


def _row_of(accounts: list[AccountRow], account_id: int) -> AccountRow:
    """The mailbox with this id, or the sentinel that stands in for none."""
    for row in accounts:
        if row.id == account_id:
            return row
    return _NO_ACCOUNT


class CredentialInput(BaseModel):
    """One generated form field, as the page renders it.

    A projection of :class:`~mailarc_core.mail.model.CredentialField` with the
    optionals resolved: Reflex warns when a ``str | None`` var lands on a prop
    that wants a string, and an empty placeholder is what "none" means here.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""


def provider_registry() -> ProviderRegistry:
    """The providers this installation registered, wherever it registered them.

    A component cannot ask ``app/composition.py`` anything, so it asks the
    registry the composition root filled. Call this inside a method only.
    """
    try:
        return service_registry().get(ProviderRegistry)
    except KeyError as error:
        raise RuntimeError(
            "No mail providers are registered — did app.composition run?"
        ) from error


def account_eraser() -> AccountEraser:
    """The clear-out, wired to the stores this installation actually uses.

    Read out of the service registry for the reason
    :func:`provider_registry` is: the composition root is the only module that
    may build one from configuration, and a page that built its own would be a
    page emptying whichever graph its defaults resolved to. Call this inside a
    method only.
    """
    try:
        return service_registry().get(AccountEraser)
    except KeyError as error:
        raise RuntimeError(
            "No account eraser is registered — did app.composition run?"
        ) from error


class MailAccountState(FieldErrors, rx.State):
    """Everything the accounts page can do, and nothing beyond it.

    Every handler here proceeds. This is a desktop archive with one person in
    front of it, so listing, connecting and deleting a mailbox needs no
    permission beyond having the application open.
    """

    accounts: list[AccountRow] = []
    selected_id: int = 0
    provider_options: list[dict[str, str]] = []
    provider: str = ""
    credential_fields: list[CredentialInput] = []
    display_name: str = ""
    email_address: str = ""
    busy: bool = False
    error: str = ""
    replacing_credentials: bool = False
    confirming_clear: bool = False
    clearing: bool = False
    cleared: str = ""

    _typed: dict[str, str] = {}
    """What the human has entered so far, keyed by credential field name.

    Backend-only: a client secret has no business being shipped back to the
    browser it came from. That is also why the credential boxes are the one
    uncontrolled part of this form — a controlled one would need its value
    here, on the wire, and in the browser's memory.
    """

    @rx.var
    def has_accounts(self) -> bool:
        return len(self.accounts) > 0

    @rx.var
    def has_credential_fields(self) -> bool:
        return len(self.credential_fields) > 0

    @rx.var
    def has_selection(self) -> bool:
        """Whether the detail column shows a mailbox or the form for a new one."""
        return self.selected_id > 0

    @rx.var
    def selected(self) -> AccountRow:
        """The chosen mailbox, or the blank row that stands in for none.

        Derived rather than stored: ``load`` re-reads every account after a
        connect or a delete, and a second copy of the row would go stale the
        moment its status changed. The id is the selection; this is the
        reading of it.
        """
        return _row_of(self.accounts, self.selected_id)

    @rx.var
    def count_label(self) -> str:
        """``3 mailboxes`` over the list, in the shape the other lists use."""
        count = len(self.accounts)
        return f"{count} mailbox" if count == 1 else f"{count} mailboxes"

    @rx.event
    async def load(self) -> None:
        """Read the providers and the accounts. The page's ``on_load``."""
        self.error = ""
        self.busy = True
        try:
            self._read_providers()
            self.accounts = await _read_accounts()
        except Exception as error:
            logger.exception("Could not load the mail accounts")
            self.error = _message(error)
        finally:
            self.busy = False

    @rx.event
    def select(self, account_id: int) -> None:
        """Show one mailbox in the detail column, with its values in the form.

        The page fires this beside
        :meth:`~mailarc_ui.imports.state.ImportJobState.select_account`, which
        is how one click both opens a mailbox and points the import at it
        without either state learning that the other exists.
        """
        self.selected_id = account_id
        self.error = ""
        self.cleared = ""
        self._clear_errors()
        self._fill_form(_row_of(self.accounts, account_id))

    @rx.event
    def start_new(self) -> None:
        """Put the empty form back in the detail column.

        Clearing the selection *is* the way to the form: the column shows a
        mailbox or it shows how to add one, and there is no third thing for a
        button to toggle.
        """
        self.selected_id = 0
        self.error = ""
        self._clear_errors()
        self._clear_form()

    @rx.event
    def replace_credentials(self) -> None:
        """Open the credential fields on a mailbox that already has a secret.

        Replacing one is a deliberate act, not something a person does by
        tabbing past a box. It is also what keeps the boxes honest: a stored
        secret is never read back into the form, so an open field that carried
        text over from the mailbox looked at a moment ago would be showing
        something this page would not save. Hidden, the fields unmount, and
        mount empty for whichever mailbox is opened next.
        """
        self.replacing_credentials = True
        self._typed = {}
        self._clear_errors()

    @rx.event
    def select_provider(self, provider: str) -> None:
        """Pick a provider and rebuild the form out of its descriptor."""
        self.provider = provider
        self._typed = {}
        self.credential_fields = _inputs_for(provider)
        # The complaints belonged to the previous provider's fields, and those
        # boxes are not on the page any more.
        self._clear_errors()

    @rx.event
    def set_credential(self, name: str, value: str) -> None:
        """Remember one typed credential field, and re-check the whole group.

        The group and not this field alone, because what makes a credential
        box required is whether *any* of them was typed into — see
        :meth:`_validate_credentials`. Typing into the last empty one has to
        clear the complaint standing under the first.
        """
        self._typed = {**self._typed, name: value}
        self._validate_credentials()

    @rx.event
    def set_display_name(self, value: str) -> None:
        """The one field with no rule: a mailbox with no name is called by its
        address, which :meth:`_create` and :meth:`_update` both do."""
        self.display_name = value

    @rx.event
    def set_email_address(self, value: str) -> None:
        self.email_address = value
        self._validate_email()

    @rx.event
    async def create_account(self) -> None:
        """Write the account row and the credential row that opens it.

        The new mailbox becomes the selected one, because adding it is never
        the last step: it still has to be connected and imported, and both of
        those live in the detail column this selection opens.
        """
        if not self._validate_form(credentials_required=True):
            return
        self.busy = True
        self.error = ""
        try:
            self.selected_id = await self._create()
            self.accounts = await _read_accounts()
            self._fill_form(_row_of(self.accounts, self.selected_id))
        except Exception as error:
            logger.exception("Could not create the mail account")
            self.error = _message(error)
        finally:
            self.busy = False

    @rx.event
    async def save_account(self) -> None:
        """Write the open mailbox's edits back — its name, address and secret.

        The same form the add half fills, pointed at a mailbox that already
        exists. What it cannot change is the provider: the stored credential
        and everything already imported belong to *that* provider, so a
        mailbox that moved to another one is a new mailbox.
        """
        if not self._validate_form(credentials_required=False):
            return
        account_id = self.selected_id
        self.busy = True
        self.error = ""
        self.cleared = ""
        try:
            await self._update(account_id)
            self.accounts = await _read_accounts()
            self._fill_form(_row_of(self.accounts, account_id))
        except Exception as error:
            logger.exception("Could not update account %d", account_id)
            self.error = _message(error)
        finally:
            self.busy = False

    @rx.event
    async def delete_account(self, account_id: int) -> None:
        """Drop an account and the secret that opened it.

        Deleting the open one puts the form back, rather than leaving the
        detail column pointed at a mailbox that no longer exists — which the
        sentinel row would draw as a blank account instead of as nothing.
        """
        self.busy = True
        self.error = ""
        try:
            await _delete(account_id)
            if self.selected_id == account_id:
                self.selected_id = 0
            self.accounts = await _read_accounts()
        except Exception as error:
            logger.exception("Could not delete account %d", account_id)
            self.error = _message(error)
        finally:
            self.busy = False

    @rx.event
    def ask_clear(self) -> None:
        """Open the confirmation for clearing the mailbox that is showing.

        A dialog rather than a second click on the button, because the two
        destructive actions on this page read almost alike and only one of
        them is reversible by re-importing. What is at stake is written out in
        the dialog, so the sentence a person confirms names the mailbox.
        """
        self.confirming_clear = True
        self.error = ""
        self.cleared = ""

    @rx.event
    def cancel_clear(self) -> None:
        """Close the confirmation without touching anything."""
        self.confirming_clear = False

    @rx.event
    def set_confirming_clear(self, opened: bool) -> None:
        """Follow the dialog when it closes itself — escape, or the overlay.

        Written out rather than left to an implicit setter, the way every
        other setter on this page is: an ``alert_dialog`` reports both
        directions through ``on_open_change``, and a handler that could *open*
        it from a stray event would put a destructive confirmation on screen
        that nobody asked for. Opening stays :meth:`ask_clear`'s job.
        """
        self.confirming_clear = self.confirming_clear and opened

    @rx.event(background=True)
    async def clear_account(self) -> None:
        """Delete everything the open mailbox has imported, keeping the mailbox.

        Background for the reason :meth:`start_consent` is: emptying a large
        mailbox is thousands of graph round trips, and a foreground handler
        would hold the state lock — and therefore every other page — for the
        whole of it. The lock is taken around the mutations only.

        The mailbox itself, its credential and its place in the list all
        survive. What goes is the import: the messages in the graph, and the
        ledgers that would otherwise make the next import skip them. Clearing
        is what :meth:`delete_account` is not — a mailbox to import again
        rather than a mailbox to forget.
        """
        async with self:
            account_id = self.selected_id
            self.confirming_clear = False
            self.clearing = True
            self.error = ""
            self.cleared = ""
        if account_id <= 0:
            async with self:
                self.clearing = False
            return
        try:
            counts = await account_eraser().erase(account_id)
            accounts = await _read_accounts()
        except AccountBusy as error:
            # Not a failure of the clear-out, and not something to retry on the
            # person's behalf: a job is writing to exactly what this would
            # delete, and the sentence says which one and what to do about it.
            logger.info("Refused to clear account %d: %s", account_id, error)
            async with self:
                self.error = _message(error)
                self.clearing = False
            return
        except Exception as error:
            logger.exception("Could not clear account %d", account_id)
            async with self:
                self.error = _message(error)
                self.clearing = False
            return
        async with self:
            self.accounts = accounts
            self.cleared = _cleared_message(counts)
            self.clearing = False

    @rx.event(background=True)
    async def start_consent(self, account_id: int) -> None:
        """Run the provider's consent step if it has one, then prove it worked.

        Two halves, and neither of them names a provider. A registration may
        carry a :data:`~mailarc_core.mail.ports.ConsentRunner` — Gmail's opens
        a browser, an app password needs none — and this asks the registry for
        one rather than importing anything: ``mailarc-ui`` may not reach a
        provider (§4.1), and the composition root is where the browser half is
        registered. Whatever the runner returns replaces the stored secret.
        Then ``verify()``, the port method that means "prove these credentials
        work", says whether it actually did.

        Background, because both halves wait on somebody else — a human at a
        consent screen, then a mailbox on the far side of the internet. The
        state lock is held around the mutations only, or every other page would
        wait on this one.
        """
        async with self:
            self.busy = True
            self.error = ""
        try:
            await _connect(account_id)
            accounts = await _read_accounts()
        except Exception as error:
            logger.exception("Consent for account %d failed", account_id)
            async with self:
                self.error = _message(error)
                self.busy = False
            return
        async with self:
            self.accounts = accounts
            self.busy = False

    # ── What makes this form valid ───────────────────────────────────────
    #
    # The rules live here rather than in `kit.validation` because what counts
    # as a mailbox address is this state's business; the kit only owns the
    # shape the answers are kept in and the way a field reads them.

    def _validate_email(self) -> bool:
        """Required, and shallowly checked for being an address at all."""
        address = self.email_address.strip()
        if not address:
            return self._check(EMAIL_FIELD, REQUIRED)
        head, _, tail = address.partition("@")
        looks_right = bool(head) and bool(tail)
        return self._check(EMAIL_FIELD, "" if looks_right else NOT_AN_ADDRESS)

    def _validate_provider(self) -> bool:
        """Only the add half can be missing one; the edit half cannot change it."""
        return self._check(PROVIDER_FIELD, "" if self.provider else PICK_A_PROVIDER)

    def _validate_credentials(self, required: bool | None = None) -> bool:
        """Every required box filled — but only once they are being written.

        ``required`` is what the two halves of this form disagree about, and
        the disagreement is load-bearing rather than cosmetic. Adding a mailbox
        writes a credential, so an empty required box is simply empty. Saving
        an existing one writes a credential **only if something was typed**,
        because the form never reads a stored secret back and a save that took
        the boxes at face value would wipe the credential of every mailbox
        whose name somebody corrected — see :meth:`_update`. So on the edit
        half the boxes are optional until one of them is touched, and required
        together from that moment on.

        Passing ``None`` means "whatever the current typing says", which is
        what the setter wants: it is called on every keystroke and cannot know
        which half of the form it is in.
        """
        writing = self._retyped() if required is None else required
        ok = True
        for field in self.credential_fields:
            if not field.required:
                continue
            filled = bool(self._typed.get(field.name, "").strip())
            if not writing:
                self._pass(field.name)
                continue
            message = "" if filled else (REQUIRED if required else HALF_A_CREDENTIAL)
            ok = self._check(field.name, message) and ok
        return ok

    def _validate_form(self, credentials_required: bool) -> bool:
        """Everything at once, for a button that is about to write.

        Every rule runs rather than the first failing one, so a form that is
        wrong in two places says both — a person who fixes one thing and is
        then told about the next has been made to submit twice to learn what
        was always on screen.
        """
        checks = [
            self._validate_email(),
            self._validate_credentials(required=credentials_required or None),
        ]
        if credentials_required:
            checks.append(self._validate_provider())
        return all(checks)

    def _read_providers(self) -> None:
        """Offer what the composition root registered, in that order."""
        descriptors = provider_registry().descriptors()
        self.provider_options = [
            {"value": descriptor.provider.value, "label": descriptor.label}
            for descriptor in descriptors
        ]
        if not self.provider and descriptors:
            self.select_provider(descriptors[0].provider.value)

    async def _create(self) -> int:
        """Both rows, in one transaction: an account without its secret is junk."""
        provider = self._chosen_provider()
        address = self.email_address.strip()
        if not address:
            raise ValueError("An email address is required.")
        secret = json.dumps(self._credential_values())

        async with get_asyncdb_session() as session:
            account = await _ACCOUNTS.create(
                session,
                MailAccountEntity(
                    provider=provider.value,
                    display_name=self.display_name.strip() or address,
                    email_address=address,
                    enabled=True,
                    status=AccountStatus.IDLE,
                ),
            )
            # A provider that asked for nothing gets no row. Gmail is that
            # provider now — its OAuth client is the installation's, not the
            # mailbox's — and an empty `{}` sitting in an encrypted column
            # would only be something for a later reader to mistake for a
            # credential.
            if self.credential_fields:
                await _CREDENTIALS.store_secret(
                    session,
                    account_id=account.id,
                    kind=FORM_CREDENTIAL_KIND,
                    secret=secret,
                )
            new_id = account.id
        logger.info("Added the %s account %s", provider.value, address)
        return new_id

    async def _update(self, account_id: int) -> None:
        """The mailbox's own fields, and its secret only where it was retyped.

        An empty credential box means *keep what is stored*. It has to: the
        form never reads a secret back — nothing here decrypts one — so a save
        that took the boxes at face value would wipe the credential of every
        mailbox whose name somebody corrected. Typing into any one of them is
        what makes this write a new secret, and then every required field must
        be filled, because half a credential opens nothing.
        """
        if account_id <= 0:
            raise ValueError("No mailbox is open.")
        address = self.email_address.strip()
        if not address:
            raise ValueError("An email address is required.")
        secret = json.dumps(self._credential_values()) if self._retyped() else ""

        async with get_asyncdb_session() as session:
            account = await _account(session, account_id)
            account.display_name = self.display_name.strip() or address
            account.email_address = address
            if secret:
                await _CREDENTIALS.store_secret(
                    session,
                    account_id=account_id,
                    kind=FORM_CREDENTIAL_KIND,
                    secret=secret,
                )
        logger.info("Updated account %d", account_id)

    def _retyped(self) -> bool:
        """Whether the human put anything into a credential field this time."""
        return any(value.strip() for value in self._typed.values())

    def _chosen_provider(self) -> MailProvider:
        if not self.provider:
            raise ValueError("Pick a provider first.")
        return MailProvider(self.provider)

    def _credential_values(self) -> dict[str, str]:
        """What the provider asked for, collected by the names it asked under.

        The dict goes into the encrypted column as JSON and is read back by the
        adapter that declared the fields (§8.1). This module never learns what
        Gmail calls its own.
        """
        values: dict[str, str] = {}
        for field in self.credential_fields:
            value = self._typed.get(field.name, "").strip()
            if field.required and not value:
                raise ValueError(f"{field.label} is required.")
            values[field.name] = value
        return values

    def _fill_form(self, row: AccountRow) -> None:
        """Put one mailbox's values into the form the detail column edits.

        The same vars the add half writes. There is one form on this page and
        two things it can be pointed at, and a second set of vars for editing
        is exactly how the two would drift apart.

        The credential fields come back closed and empty — see
        :meth:`replace_credentials`.
        """
        self.display_name = row.display_name
        self.email_address = row.email_address
        self.provider = row.provider
        self.credential_fields = _fields_of(row.provider)
        self.replacing_credentials = False
        self._typed = {}

    def _clear_form(self) -> None:
        self.display_name = ""
        self.email_address = ""
        self.replacing_credentials = False
        self._typed = {}


def _cleared_message(counts: EraseCounts) -> str:
    """What a person reads after a clear-out. Never empty.

    Counts rather than "done", because the number is the confirmation: a
    mailbox that reported ten thousand imported messages and clears one is a
    mailbox where something else is wrong. ``copies`` is mentioned only when
    there were any — it is the surprising case, mail that stays in the archive
    because another mailbox holds it too, and a nought would only invite the
    question.
    """
    if counts.messages == 0 and counts.copies == 0:
        return "There was nothing imported to clear."
    cleared = f"Cleared {_messages(counts.messages)}."
    if counts.copies == 0:
        return cleared
    stay = "stays" if counts.copies == 1 else "stay"
    return (
        f"{cleared} {_messages(counts.copies)} {stay} in the archive under "
        "another mailbox."
    )


def _messages(count: int) -> str:
    """``1 message`` or ``42 messages`` — the noun both sentences share."""
    return f"{count} message" if count == 1 else f"{count} messages"


def _inputs_for(provider: str) -> list[CredentialInput]:
    """The form fields one provider declared; none while nothing is picked."""
    if not provider:
        return []
    descriptor = provider_registry().descriptor_for(MailProvider(provider))
    return [
        CredentialInput(
            name=field.name,
            label=field.label,
            secret=field.secret,
            required=field.required,
            placeholder=field.placeholder or "",
        )
        for field in descriptor.credential_fields
    ]


def _fields_of(provider: str) -> list[CredentialInput]:
    """The fields of a provider that may not be registered in this process.

    :class:`AccountRow` keeps the short provider name precisely so a mailbox
    whose adapter this installation left out is still listed and can still be
    deleted. The form beside it then renders without its credential half
    rather than refusing to render at all.
    """
    try:
        return _inputs_for(provider)
    except ValueError, KeyError, RuntimeError:
        logger.debug("No credential fields for the provider %s", provider)
        return []


async def _read_accounts() -> list[AccountRow]:
    """Every account, projected while its session is still open."""
    async with get_asyncdb_session() as session:
        entities = await _ACCOUNTS.find_all(session)
        rows = [
            AccountRow(
                id=entity.id,
                provider=entity.provider,
                display_name=entity.display_name,
                email_address=entity.email_address,
                status=entity.status,
                status_color=_STATUS_COLORS.get(entity.status, "gray"),
                enabled=entity.enabled,
                last_error=entity.last_error or "",
            )
            for entity in entities
        ]
    return sorted(rows, key=lambda row: row.id)


async def _delete(account_id: int) -> None:
    """Remove the account and its secrets.

    The credentials go explicitly rather than by ``ON DELETE CASCADE``: SQLite
    honours the constraint only where ``PRAGMA foreign_keys`` was set, and a
    secret outliving the mailbox it opened is not something to leave to a
    connection setting.
    """
    async with get_asyncdb_session() as session:
        for credential in await _credentials_of(session, account_id):
            await _CREDENTIALS.delete(session, credential)
        await _ACCOUNTS.delete_by_id(session, account_id)
    logger.info("Deleted account %d", account_id)


async def _connect(account_id: int) -> None:
    """Grant, then prove — and write down whichever half failed.

    The outcome goes into its own transaction: a failure has to survive the
    exception that carries it, and ``auth_error`` is what makes the page offer
    another consent instead of a silent retry.
    """
    try:
        await _grant(account_id)
        identity = await _verify(account_id)
        await _require_same_mailbox(account_id, identity)
    except Exception as error:
        await _record(account_id, AccountStatus.AUTH_ERROR, _message(error))
        raise
    logger.info("Account %d is connected as %s", account_id, identity.address.address)
    await _record(account_id, AccountStatus.IDLE, None)


async def _grant(account_id: int) -> None:
    """Run the provider's consent step, if it registered one, and keep it.

    The runner is awaited *outside* any session: it waits on a human at a
    consent screen, and a database transaction held open for that is a
    transaction held open for minutes.

    What it returns supersedes what the user typed, so the typed row goes —
    the new secret carries those same fields plus what consent earned, and one
    copy of a credential is better than two.
    """
    async with get_asyncdb_session() as session:
        account = await _account(session, account_id)
        runner = provider_registry().consent_for(MailProvider(account.provider))
        if runner is None:
            return
        typed = _values_of(await _credentials_of(session, account_id))
        # The address is what a browser-based runner hands the identity
        # provider as the login hint, so the consent opens on this mailbox
        # rather than on whichever account the browser happens to favour.
        typed[CONSENT_ADDRESS_KEY] = account.email_address

    secret = await runner(typed)

    async with get_asyncdb_session() as session:
        for stale in await _credentials_of(session, account_id):
            await _CREDENTIALS.delete(session, stale)
        await _CREDENTIALS.store_secret(
            session,
            account_id=account_id,
            kind=CredentialKind.OAUTH,
            secret=secret,
        )
    logger.info("Consent completed for account %d", account_id)


async def _verify(account_id: int) -> AccountIdentity:
    """Let the mailbox say whose it is. Closes the source either way.

    The source is built while the account row is still live and used after the
    session closed — the same order ``app/worker.py`` uses, because a factory
    reads what it needs off the row and a closed session hands back nothing.
    """
    async with get_asyncdb_session() as session:
        source = await _open(session, account_id)
    try:
        return await source.verify()
    finally:
        await source.aclose()


async def _require_same_mailbox(account_id: int, identity: AccountIdentity) -> None:
    """The mailbox that answered must be the one the row names.

    A consent screen lets a person pick any account they are signed in to, and
    a token for the wrong one would archive somebody else's mail under this
    row — silently, on every run. So the token goes, and the sentence says
    which account to pick next time.
    """
    actual = identity.address.address
    async with get_asyncdb_session() as session:
        expected = (await _account(session, account_id)).email_address
        if _same_address(actual, expected):
            return
        for credential in await _credentials_of(session, account_id):
            await _CREDENTIALS.delete(session, credential)
    raise MailAuthError(
        f"Signed in as {actual}, but this account is {expected.strip()} — "
        f"connect again and pick {expected.strip()}"
    )


def _same_address(left: str, right: str) -> bool:
    """Addresses compare case-insensitively; the row may carry stray spaces."""
    return left.strip().lower() == right.strip().lower()


def _values_of(credentials: list[MailCredentialEntity]) -> dict[str, str]:
    """The stored credential as the flat mapping a consent runner expects.

    Reads the first row an account has, which is the typed one before a
    consent and the granted one after — and both spell the fields the provider
    declared under the same names, so a second consent re-uses what the first
    one was given rather than asking a human to type it again.
    """
    for credential in credentials:
        try:
            values = json.loads(credential.secret)
        except ValueError:
            continue
        if isinstance(values, dict):
            return {str(key): str(value) for key, value in values.items()}
    return {}


async def _account(session: AsyncSession, account_id: int) -> MailAccountEntity:
    """The account row, or the sentence that says it is gone."""
    account = await _ACCOUNTS.find_by_id(session, account_id)
    if account is None:
        raise LookupError(f"account {account_id} is gone")
    return account


async def _open(session: AsyncSession, account_id: int) -> MailSourcePort:
    """The mailbox behind one account, through the registry and the port."""
    account = await _account(session, account_id)
    secret = await _secret_of(session, account_id)
    factory = provider_registry().factory_for(MailProvider(account.provider))
    return factory(account, secret)


async def _secret_of(session: AsyncSession, account_id: int) -> str:
    """The account's stored secret, whichever kind it turned out to be."""
    for credential in await _credentials_of(session, account_id):
        return credential.secret
    raise LookupError(f"account {account_id} has no stored credential")


async def _credentials_of(
    session: AsyncSession, account_id: int
) -> list[MailCredentialEntity]:
    """Every credential an account has; there is at most one per kind."""
    found = [
        await _CREDENTIALS.find_by_account(session, account_id, kind)
        for kind in CredentialKind
    ]
    return [credential for credential in found if credential is not None]


async def _record(account_id: int, status: AccountStatus, detail: str | None) -> None:
    """Note how the last attempt at this mailbox went."""
    async with get_asyncdb_session() as session:
        account = await _ACCOUNTS.find_by_id(session, account_id)
        if account is None:
            return
        account.status = status
        account.last_error = detail


def _message(error: Exception) -> str:
    """What a human reads when something failed. Never empty."""
    return str(error) or type(error).__name__
