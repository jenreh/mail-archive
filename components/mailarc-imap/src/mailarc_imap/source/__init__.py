"""IMAP behind :class:`~mailarc_core.mail.ports.MailSourcePort`.

The package is named after the capability, not the protocol —
``mailarc_google.source`` and ``mailarc_m365.source`` look identical — and it
is the whole of what this component does. IMAP's UIDs, its modified-UTF-7
folder names and its flags stop here; what leaves is the vocabulary of
:mod:`mailarc_core.mail.model`.

One module per concern, layered so nothing points back up:

``model``
    IMAP's own shapes — the ports, the well-known hosts, ``UIDVALIDITY`` and
    what ``EXAMINE`` says — plus ``IMAP_DESCRIPTOR``, the one declaration that
    faces the domain and the place the one-folder-per-account decision is
    argued.
``config``
    ``ImapConfig`` — two timeouts, a page size and one switch for the test
    suite. No account, and no host: an IMAP host belongs to the mailbox.
``credentials``
    ``ImapCredentials`` — what fills ``mail_credentials.secret``, and the
    parsing that accepts the account form's all-strings JSON without ever
    quoting a password back.
``client``
    The blocking IMAP conversation — one connection, one selected folder, one
    lock — and the only place a socket or a ``NO`` becomes one of the four
    errors.
``mapping``
    IMAP's numbers turned into domain value objects, and no integer out. The
    cursor and the message id are both minted and read here and nowhere else.
``source``
    ``ImapSource`` — the six methods of the port, made of the rest.
"""

from mailarc_imap.source.client import ImapClient
from mailarc_imap.source.config import ImapConfig
from mailarc_imap.source.credentials import ImapCredentials
from mailarc_imap.source.model import (
    DEFAULT_FOLDER,
    GMAIL_ALL_MAIL,
    GMAIL_IMAP_HOST,
    ICLOUD_IMAP_HOST,
    IMAP_DESCRIPTOR,
    IMAPS_PORT,
    FetchedBody,
    FolderListing,
    FolderState,
)
from mailarc_imap.source.source import ImapSource

__all__ = [
    "DEFAULT_FOLDER",
    "GMAIL_ALL_MAIL",
    "GMAIL_IMAP_HOST",
    "ICLOUD_IMAP_HOST",
    "IMAPS_PORT",
    "IMAP_DESCRIPTOR",
    "FetchedBody",
    "FolderListing",
    "FolderState",
    "ImapClient",
    "ImapConfig",
    "ImapCredentials",
    "ImapSource",
]
