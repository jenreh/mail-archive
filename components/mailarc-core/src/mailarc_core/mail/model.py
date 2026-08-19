"""What a mailbox and a message are, before anyone stores or fetches them.

Pure value objects: no I/O, no provider vocabulary. A Gmail JSON blob never
reaches the graph — an adapter turns it into the types below, and everything
downstream only ever sees these. That is the whole anti-corruption layer.

All of them are frozen pydantic models, so a page of results cannot be edited
after the fact and a value that came from a provider is validated on the way
in.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class MailProvider(StrEnum):
    """Which kind of mailbox a message came out of.

    ``FAKE`` is not a placeholder — it is the in-memory source the engine
    tests run against, and it keeps the port honest from day one.
    """

    GMAIL = "gmail"
    IMAP = "imap"
    M365 = "m365"
    FAKE = "fake"


class LabelKind(StrEnum):
    """Where a label came from.

    ``SYSTEM`` is the provider's own (INBOX, SENT), ``USER`` is one a human
    made, ``FOLDER`` is an IMAP mailbox pretending to be a label. Derived
    ``Topic`` nodes are none of these on purpose: a guess never becomes a
    label.
    """

    SYSTEM = "system"
    USER = "user"
    FOLDER = "folder"


class SyncCursorKind(StrEnum):
    """Whether a cursor walks the whole mailbox or only what changed."""

    FULL = "full"
    INCREMENTAL = "incremental"


class EmailAddress(BaseModel):
    """One address, normalised to the form the graph uses as a key.

    Lowercased whole, angle brackets gone: the same human writing from
    ``Bob@Example.COM`` and ``bob@example.com`` has to land on one node. RFC
    5322 lets the local part be case-sensitive; no mail system in practice
    treats it that way, and splitting the node would be worse than the
    theoretical loss.

    ``display_name`` is *this* sighting's name only. The graph collects them
    all on the node, because the same address signs itself differently.
    """

    model_config = ConfigDict(frozen=True)

    address: str
    display_name: str | None = None

    @field_validator("address", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip().strip("<>").strip().lower()

    @property
    def local_part(self) -> str:
        """Everything before the last ``@``, or the whole thing if there is none."""
        local, separator, _ = self.address.rpartition("@")
        return local if separator else self.address

    @property
    def domain(self) -> str:
        """Everything after the last ``@``; empty for a malformed address."""
        _, separator, domain = self.address.rpartition("@")
        return domain if separator else ""


class LabelInfo(BaseModel):
    """A label as the provider describes it, before it becomes a node."""

    model_config = ConfigDict(frozen=True)

    provider_label_id: str
    name: str
    kind: LabelKind = LabelKind.USER
    message_count: int | None = None


class AccountIdentity(BaseModel):
    """Who the credentials we just used actually belong to.

    What ``verify()`` returns. Worth its own type because the answer is
    routinely *not* what the user typed into the form — an alias, a shared
    mailbox, or a second account they were still logged into.
    """

    model_config = ConfigDict(frozen=True)

    provider: MailProvider
    address: EmailAddress
    display_name: str | None = None
    provider_account_id: str | None = None


class MessageRef(BaseModel):
    """A message the provider has, cheap enough to list thousands of.

    Listing hands these back; fetching bodies is a separate, expensive round
    trip. ``size_estimate`` is the provider's own guess and exists so a run
    can be budgeted before it starts.
    """

    model_config = ConfigDict(frozen=True)

    provider_message_id: str
    provider_thread_id: str | None = None
    labels: tuple[str, ...] = ()
    size_estimate: int | None = None


class RawMessage(BaseModel):
    """The RFC 5322 bytes, still exactly as the provider sent them.

    Kept whole rather than pre-parsed: the bytes are what gets hashed for
    ``eml_sha256`` and what goes to the blob store, so a parser fix can be
    replayed over the archive without asking the provider again.
    """

    model_config = ConfigDict(frozen=True)

    ref: MessageRef
    raw: bytes


class SyncCursor(BaseModel):
    """Where the last run got to — **opaque to the engine**.

    Gmail puts a ``historyId`` in ``token``, IMAP a ``UIDVALIDITY/UIDNEXT``
    pair, MS Graph a ``deltaLink``. The engine never looks inside; it stores
    the token and hands it back. That is what keeps the port from growing a
    provider-shaped hole.
    """

    model_config = ConfigDict(frozen=True)

    provider: MailProvider
    token: str
    kind: SyncCursorKind = SyncCursorKind.FULL


class MessagePage(BaseModel):
    """One page of listing results.

    Paging is the adapter's business: the engine only ever asks "is there a
    next cursor". ``estimated_total`` drives the progress bar and is allowed
    to be wrong.
    """

    model_config = ConfigDict(frozen=True)

    refs: tuple[MessageRef, ...] = ()
    next_cursor: SyncCursor | None = None
    estimated_total: int | None = None


class ParsedAttachment(BaseModel):
    """One attachment, identified by content rather than by name.

    ``sha256`` is the key: the same file on two messages is one node with two
    edges, and the filename hangs on the edge because the sender renamed it.
    """

    model_config = ConfigDict(frozen=True)

    filename: str | None = None
    content_type: str = "application/octet-stream"
    size: int = 0
    sha256: str = ""
    content_id: str | None = None
    inline: bool = False
    payload: bytes = b""


class ParsedMessage(BaseModel):
    """A message reduced to every field the ``Message`` node carries.

    Nothing here is guessed: each field is read out of the message itself or
    computed from it deterministically, so re-parsing the same bytes gives the
    same node.

    ``body_text`` and ``body_clean`` are two different fields and both are
    needed. ``body_text`` is the full text and feeds full-text search;
    ``body_clean`` has the quoted predecessors, the sign-off and the legal
    footer removed and feeds the SimHash and the embedding. Skip the cleaning
    and every message sharing a company footer looks like the same template.
    """

    model_config = ConfigDict(frozen=True)

    canonical_id: str
    rfc_message_id: str | None = None
    subject: str = ""
    subject_norm: str = ""
    sent_at: datetime | None = None
    sender: EmailAddress | None = None
    to: tuple[EmailAddress, ...] = ()
    cc: tuple[EmailAddress, ...] = ()
    bcc: tuple[EmailAddress, ...] = ()
    body_text: str = ""
    body_clean: str = ""
    simhash: int = 0
    participant_key: str = ""
    refs: tuple[str, ...] = ()
    size_bytes: int = 0
    has_attachments: bool = False
    eml_sha256: str = ""
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[ParsedAttachment, ...] = ()
    thread_hint: str | None = None
    """Thread root guessed from the headers, for providers that have no threads.

    The provider's own thread id always wins where there is one; this is what
    IMAP has instead.
    """

    @property
    def participants(self) -> tuple[EmailAddress, ...]:
        """Everyone on the message: sender first, then to, cc and bcc."""
        sender = (self.sender,) if self.sender else ()
        return sender + self.to + self.cc + self.bcc


class CredentialField(BaseModel):
    """One input a provider needs before it can connect.

    Declared rather than coded so the account form is generated: adding IMAP
    must not mean writing a second form.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str | None = None


class ProviderDescriptor(BaseModel):
    """What a provider is called and what it needs — registry and form in one.

    The registry keys on ``provider``; the account form renders
    ``credential_fields``. One declaration, so the two can never drift apart.
    """

    model_config = ConfigDict(frozen=True)

    provider: MailProvider
    label: str
    credential_fields: tuple[CredentialField, ...] = ()
    supports_incremental: bool = False
