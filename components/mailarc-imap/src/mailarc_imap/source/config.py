"""How hard this installation may lean on an IMAP server, and how long it waits.

Nothing here names a mailbox, and this component had to work harder than Gmail
to keep that true. Gmail's endpoints belong to the installation; an IMAP host
belongs to the account, so ``host``, ``port``, ``username``, ``password`` and
``folder`` are :class:`~mailarc_imap.source.credentials.ImapCredentials` and
sit in the encrypted column with the rest of the mailbox's state (§8.1). §4.2's
rule is the test: a second IMAP account must not need a second config, and an
archive holding an iCloud mailbox and a Gmail-app-password mailbox holds two
hosts at once.

What is left is genuinely per-installation: two timeouts, a page size and the
certificate authority to trust.

The well-known hosts a form would prefill are **not** here either, and that
does diverge from what a reader might expect. They are
:data:`~mailarc_imap.source.model.ICLOUD_IMAP_HOST` and
:data:`~mailarc_imap.source.model.GMAIL_IMAP_HOST`, module constants, because
the thing that prefills the form is
:data:`~mailarc_imap.source.model.IMAP_DESCRIPTOR` — itself a module constant,
built at import time, with no configuration object in scope and no way to reach
one. A setting nothing can read is not a setting.
"""

from appkit_commons.configuration.base import BaseConfig
from pydantic_settings import SettingsConfigDict


class ImapConfig(BaseConfig):
    """Two deadlines, a page size, and which certificate authority to believe."""

    model_config = SettingsConfigDict(
        env_prefix="app_imap_",
        env_file=".env",
        populate_by_name=True,
    )

    connect_timeout: float = 15.0
    """Seconds the TCP connect and the TLS handshake together may take.

    Short, because there is nothing to wait for: a host that has not answered
    in fifteen seconds is a typo in the account form or a network that is down,
    and both are better reported than sat on. Separate from
    :attr:`request_timeout` for the reason IMAP makes it matter — a connection
    is opened once per run and then used for thousands of fetches, so the two
    numbers are answering different questions.
    """

    request_timeout: float = 120.0
    """Seconds any single IMAP command may take before it counts as transient.

    Generous on purpose. ``UID SEARCH`` over a mailbox with a hundred thousand
    messages is one command and one answer, and a large attachment arrives as
    one literal on the same socket — neither is chunked, so a timeout that
    suits an HTTP request would turn an ordinary big message into a permanent
    failure. Finite all the same, so a half-open socket becomes a retry rather
    than a worker stuck until someone notices.
    """

    page_size: int = 200
    """UIDs handed back in one listing page.

    ``UID SEARCH`` has no server-side paging — it answers with every matching
    UID in one line — so this is the size of the slice this adapter cuts off
    that answer, not a limit on the search. Twice Gmail's hundred because the
    call it pages is one already-paid-for round trip rather than a fresh HTTPS
    request each time, and still small enough that a cancelled import stops
    promptly.
    """

    tls_ca_file: str = ""
    """A PEM bundle to verify the server certificate against, instead of the system's.

    Empty means the platform trust store, which is what iCloud and Gmail need
    and what every hosted mail provider needs. It is a setting for the mail
    server somebody runs themselves behind a private certificate authority —
    and for this suite, which serves a self-signed certificate over a loopback
    socket so that the TLS path a real account takes is the path the tests take
    (§10 phase 8: no test talks to a real provider).

    **There is no switch to turn TLS off.** Not an omission: an app password in
    the clear on a shared network is the credential itself, so plaintext IMAP
    is a downgrade this adapter does not offer. It is also the one thing
    ``imapclient`` 3.1.0 cannot do on Python 3.14 — its ``IMAP4WithTimeout``
    assigns ``self.file``, which became a read-only property, so the plaintext
    path raises ``AttributeError`` before it sends a byte. TLS-only is the
    right answer on its own merits and happens to also be the working one.
    """
