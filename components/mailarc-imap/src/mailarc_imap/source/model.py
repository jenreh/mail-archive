"""IMAP's own shapes, and the one declaration that faces the domain.

Everything RFC 3501 names differently from the rest of the system stops in this
file: ``UIDVALIDITY``, ``UIDNEXT``, the ``\\Noselect`` flag, the two well-known
hosts a person is most likely to type. The vocabulary everyone else shares
lives in :mod:`mailarc_core.mail.model`, and nothing below may leak into it.

:data:`IMAP_DESCRIPTOR` is the exception that faces both ways. It is a domain
value object, and it is the only place that says what an IMAP account needs
before it can connect — the account form renders those fields and
``app/composition.py`` registers the descriptor. One declaration, so the form
and the registry cannot drift apart.

The host and the port are **credential fields rather than configuration**, and
that is the one place this provider's layout differs from Gmail's. Gmail's
endpoints belong to the installation, so a second Gmail account needs no second
setting; an IMAP host belongs to the *mailbox*, and an archive that holds an
iCloud account and a Gmail-over-app-password account holds two different hosts
at once. §4.2's rule is that a second account must not need a second config,
which is exactly why the host cannot live in one.
"""

from pydantic import BaseModel, ConfigDict

from mailarc_core.mail.model import (
    CredentialField,
    MailProvider,
    ProviderDescriptor,
)

IMAPS_PORT = 993
"""The implicit-TLS port, and the only one this adapter is built around.

Port 143 with ``STARTTLS`` is the other legal way to reach an IMAP server and
is deliberately not offered: it means a plaintext greeting an attacker can
strip the ``STARTTLS`` capability out of, and every host this component targets
has served 993 for a decade. A user who types 143 into the form gets a
connection this adapter will refuse to secure, not a silent downgrade.
"""

ICLOUD_IMAP_HOST = "imap.mail.me.com"
"""iCloud Mail. Needs an app-specific password; the Apple ID password is refused."""

GMAIL_IMAP_HOST = "imap.gmail.com"
"""Gmail over IMAP, for an account that cannot or will not use OAuth.

The Gmail *adapter* (``mailarc-google``) is the better route — it has a real
delta and needs no password stored anywhere. This one exists for the mailbox
whose owner has an app password and no wish to register an OAuth client.
"""

GMAIL_ALL_MAIL = "[Gmail]/All Mail"
"""The one Gmail folder that holds every message exactly once.

Gmail's per-label folders are *views*: a message labelled ``Work`` appears in
``[Gmail]/All Mail`` and in ``Work``, with a different UID in each, and IMAP
offers no way to notice they are the same message. Pointing this adapter at
``INBOX`` on a Gmail account therefore archives the inbox and nothing else,
while pointing it at every folder in turn would archive most messages several
times. See :data:`IMAP_DESCRIPTOR` for what this component does instead.
"""

DEFAULT_FOLDER = "INBOX"
"""The one folder name RFC 3501 requires every server to have.

Right for iCloud and for a plain mail host, wrong for Gmail — which is why the
field is offered with this as its placeholder rather than silently assumed.
"""

NOSELECT_FLAG = rb"\Noselect"
"""``LIST`` flag for a name that is a container and not a mailbox.

``[Gmail]`` itself carries it, and so does every intermediate node of a
hierarchy on a server that models folders as a tree. Offering one as a
synchronisable folder would hand the user a name that answers ``SELECT`` with
``NO``.
"""


class FolderListing(BaseModel):
    """One row of a ``LIST`` reply, before it becomes a label.

    ``flags`` stays as the bytes the server sent. IMAP flag names are
    case-insensitive ASCII and the only one this adapter reads is
    :data:`NOSELECT_FLAG`; decoding them to ``str`` here would buy a nicer repr
    and one more place for a comparison to go wrong.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    flags: tuple[bytes, ...] = ()
    delimiter: str | None = None

    def selectable(self) -> bool:
        """Whether ``SELECT`` on this name would work at all."""
        return not any(flag.lower() == NOSELECT_FLAG.lower() for flag in self.flags)


class FolderState(BaseModel):
    """What ``EXAMINE`` says about the folder, reduced to the three numbers used.

    ``uidvalidity`` is the generation of the folder's UID space: the server
    promises that while it stays the same, a UID means the same message
    forever, and that when it changes every UID the archive has stored means
    nothing. ``uidnext`` is the UID the *next* arrival will get, which is
    precisely what a watermark is — everything from here up is new.
    ``exists`` is the message count, kept only to estimate a progress bar.
    """

    model_config = ConfigDict(frozen=True)

    folder: str
    uidvalidity: int
    uidnext: int
    exists: int = 0


class FetchedBody(BaseModel):
    """One message's RFC 5322 bytes, as ``UID FETCH`` handed them over.

    ``size`` is the server's ``RFC822.SIZE`` and not ``len(raw)``: the two
    disagree on servers that store messages with bare-LF line endings, and the
    domain's ``size_estimate`` is documented as the provider's own guess.
    """

    model_config = ConfigDict(frozen=True)

    uid: int
    raw: bytes
    size: int | None = None


IMAP_DESCRIPTOR = ProviderDescriptor(
    provider=MailProvider.IMAP,
    label="IMAP (iCloud, Gmail app password, any mail host)",
    credential_fields=(
        CredentialField(
            name="host",
            label="IMAP server",
            placeholder=ICLOUD_IMAP_HOST,
        ),
        CredentialField(
            name="port",
            label="Port",
            required=False,
            placeholder=str(IMAPS_PORT),
        ),
        CredentialField(
            name="username",
            label="Username (must match the email address above)",
            placeholder="you@icloud.com",
        ),
        CredentialField(
            name="password",
            label="App-specific password",
            secret=True,
        ),
        CredentialField(
            name="folder",
            label=f"Folder (Gmail: {GMAIL_ALL_MAIL})",
            required=False,
            placeholder=DEFAULT_FOLDER,
        ),
    ),
    supports_incremental=True,
)
"""What an IMAP account needs from the user, and what it can do once it has it.

Five fields, and the first provider whose form is not empty — Gmail's OAuth
client belongs to the installation, so its descriptor declares nothing. Two of
the five are optional and fall back to :data:`IMAPS_PORT` and
:data:`DEFAULT_FOLDER`, because a blank field is what an account form sends for
"I did not type anything here" and a person adding an iCloud mailbox should
have to fill in three boxes rather than five.

**One folder per account, and that is the answer to the duplication question.**
An IMAP UID identifies a message inside one folder and inside one
``UIDVALIDITY``, and nothing else: the same message in ``INBOX`` and in
``Archive`` has two unrelated UIDs, and IMAP will not say they are one message.
An adapter that walked every folder would therefore need a cursor per folder
and would archive a Gmail mailbox roughly five times over — once per label —
which is worse than archiving none of it. So the mailbox this account syncs is
a *field*, a second folder is a second account, and the cursor stays the single
``UIDVALIDITY``/``UIDNEXT`` pair §10 asks for. Gmail users point it at
:data:`GMAIL_ALL_MAIL`, which is the folder Google maintains for exactly this.

That also settles ``\\Junk`` and ``\\Trash`` without a rule about them. Gmail's
``messages.list`` excludes spam and trash by default and
``GmailSource.list_messages`` explains why — an archive of a mailbox is what
the user sees in it. Here nothing is imported from a folder the user did not
name, so the exclusion is already in force and a second one would only stop
somebody deliberately archiving their own spam folder.
:meth:`~mailarc_imap.source.source.ImapSource.list_labels` still reports both,
because that method describes the mailbox rather than the import, and a folder
list that silently omitted two names would be a list nobody could pick from.

**The username's label carries a warning rather than a name**, and it is not
decoration. ``mailarc_ui.accounts.state`` verifies a new mailbox by calling
``verify()`` and then comparing the
:class:`~mailarc_core.mail.model.AccountIdentity` it answered with against the
address on the account row — and when the two differ it **deletes the stored
credential** before it says so, because for an OAuth provider a mismatch means
the consent screen signed the user in as somebody else. IMAP has no consent
screen and no ``getProfile``, so the only address
:func:`~mailarc_imap.source.mapping.identity` can report is the authenticated
username. A person whose mail host issues a username that is not their address
— ``jens`` rather than ``jens@example.com``, which plenty of hosts do — would
therefore fill the form in correctly, press Connect and have the password wiped
with a sentence about picking a different account. The label says so on the form
because this component cannot say it anywhere else: the comparison lives in a
module a provider may not touch.

``supports_incremental`` is **true**, and
:meth:`~mailarc_imap.source.source.ImapSource.watermark` is what has to agree
with it: it reads ``UIDVALIDITY``/``UIDNEXT`` off the selected folder and never
answers ``None``. The pairing is not decorative — the interval scheduler queues
a delta for every account whose provider claims one, so a descriptor promising
a delta while ``watermark()`` answered ``None`` would be a mailbox scheduled
forever that fetched nothing, and no other component is in a position to
notice.
"""
