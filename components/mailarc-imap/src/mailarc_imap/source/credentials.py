"""What fills ``mail_credentials.secret`` for IMAP, and the shape it arrives in.

§8.1 keeps that column structureless on purpose: every provider serialises its
own model into it, so a new one costs no migration. :class:`ImapCredentials` is
IMAP's, which makes the round trip through JSON this module's job rather than
the persistence layer's.

**This is the first provider whose secret is written by the account form**, and
that is the whole reason the parsing below is not one ``model_validate_json``
call. Gmail's secret is minted by a consent runner and comes back out of
``GmailCredentials.to_secret()``, so its types survive the round trip. IMAP has
no consent runner — an app password is complete the moment it is typed, which
is the case :data:`~mailarc_core.mail.ports.ConsentRunner` says the port does
not cover — so what lands in the column is
``json.dumps({field.name: typed_value})`` over
:data:`~mailarc_imap.source.model.IMAP_DESCRIPTOR`'s own fields, built by
``mailarc_ui.accounts.state``. **Every value in it is a string**, ``port``
included, and an optional field the user left alone arrives as ``""`` rather
than absent.

Nothing rotates here. There is no refresh, no expiry and no second round trip:
an app password is the credential until a human replaces it. The
:meth:`ImapCredentials.to_secret` half exists so the worker's rotation check
finds a value that never changes and writes nothing, rather than finding
nothing and having to special-case this provider.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mailarc_core.mail.errors import MailAuthError
from mailarc_imap.source.model import IMAPS_PORT

MAX_PORT = 65535
"""The largest number a TCP port can be. Anything above it is a typo, not a port."""


class ImapCredentials(BaseModel):
    """Everything needed to open one IMAP mailbox — the mailbox included.

    Unlike :class:`~mailarc_google.source.credentials.GmailCredentials`, which
    holds only what is true of the account and leaves the endpoint to
    configuration, this carries ``host`` and ``port`` as well. That is not an
    inconsistency: Gmail's endpoint is the installation's and IMAP's is the
    mailbox's, and putting a host in :class:`~mailarc_imap.source.config.ImapConfig`
    would mean a deployment could hold exactly one IMAP account.

    There is no ``folder``. A walk covers the whole account
    (:data:`~mailarc_imap.source.model.IMAP_DESCRIPTOR` has the argument), so
    the field the form used to ask for is gone. A row written before that
    change still has one in its JSON and is read without complaint: pydantic
    ignores unknown keys by default, which is exactly the migration behaviour
    an opaque credential column exists to give.

    Frozen, like every other value object here, though nothing refreshes it.
    """

    model_config = ConfigDict(frozen=True)

    host: str
    username: str
    password: str = Field(repr=False)
    """Never in a repr. ``model_dump_json`` still writes it — that is the point
    of the column — but a frozen model ends up in tracebacks, log lines and
    debugger frames, and the one field here that must not appear in any of them
    is the one a human typed out of their password manager."""

    port: int = IMAPS_PORT

    @model_validator(mode="before")
    @classmethod
    def _drop_blanks(cls, value: Any) -> Any:
        """An untyped optional field is absent, not empty.

        ``mailarc_ui.accounts.state`` writes every declared field, so a port
        the user did not fill in arrives as ``""`` and not as a missing key.
        Left alone that is an ``int_parsing`` error on a form the user filled
        in correctly. Dropping the blanks here — before the field defaults are
        applied — is what makes ``required=False`` on a
        :class:`~mailarc_core.mail.model.CredentialField` mean what it says.

        Blanks only. A ``host`` of ``""`` still fails, as a missing required
        field should.
        """
        if not isinstance(value, dict):
            return value
        optional = {"port"}
        return {
            key: item
            for key, item in value.items()
            if not (key in optional and isinstance(item, str) and not item.strip())
        }

    @field_validator("host", "username", mode="after")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        """A hostname with a space around it is the same hostname.

        Typed into a form or pasted out of a support article, and a leading
        space in ``host`` becomes a DNS lookup that fails with a sentence about
        name resolution rather than about the form. ``password`` is deliberately
        **not** in this list: a trailing space may be part of it, and silently
        removing one would produce a login failure nobody could explain.

        What is left of nothing is still nothing, and that fails here rather
        than at a socket. ``_drop_blanks`` has already turned an untouched
        *optional* field into an absent one, so anything empty arriving at this
        point is a required field the caller filled in with whitespace.
        """
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be empty")
        return trimmed

    @field_validator("port", mode="after")
    @classmethod
    def _a_real_port(cls, value: int) -> int:
        """A port outside 1–65535 cannot be dialled, so it fails as a credential."""
        if not 1 <= value <= MAX_PORT:
            raise ValueError("port must be between 1 and 65535")
        return value

    def to_secret(self) -> str:
        """Serialise into ``mail_credentials.secret``, which encrypts it.

        Read back by :meth:`from_secret` and compared, unchanged, by
        ``app/worker.py``'s rotation check at the end of every run. IMAP has
        nothing to rotate, so the comparison always matches and nothing is
        written — which is the behaviour that method wants from a provider with
        nothing to say.
        """
        return self.model_dump_json()

    @classmethod
    def from_secret(cls, secret: str) -> ImapCredentials:
        """Read back either shape: what :meth:`to_secret` wrote, or the form's.

        A row that does not parse is a credential this process cannot use, so
        it fails as one rather than as a ``ValidationError`` nobody upstream
        knows what to do with.

        **The validation error never reaches the message.** pydantic appends
        ``input_value=`` to every complaint, and the input here is the account's
        app password — so interpolating it would copy a password out of the
        encrypted column into ``mail_accounts.last_error``, into
        ``mail_sync_jobs.error``, onto the page and into the log, none of which
        are encrypted. ``from error`` keeps the detail on the traceback for
        whoever is holding a debugger; the sentence a human reads carries no
        part of the credential. Copied from
        ``mailarc_google.source.credentials.from_secret``, which learned it
        first.
        """
        try:
            return cls.model_validate_json(secret)
        except ValueError as error:
            raise MailAuthError(
                "the stored IMAP credentials are unreadable or incomplete — "
                "check the server, username and password on this mailbox"
            ) from error
