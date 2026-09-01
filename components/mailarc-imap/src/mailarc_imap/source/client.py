"""The IMAP conversation, and the only place a protocol failure gets a meaning.

Three jobs and no fourth: hold one authenticated connection with one folder
selected, turn what the server or the socket does into the taxonomy of §7.6,
and let go of it afterwards. What a UID *means* is
:mod:`~mailarc_imap.source.mapping`'s business; which command to send is
:mod:`~mailarc_imap.source.source`'s.

**Nothing but the four errors leaves this module.** Not an ``imapclient``, an
``imaplib``, a ``socket`` or an ``ssl`` exception — and not a ``ValueError``
either, which is the one that hides: ``imapclient`` decodes folder names from
modified UTF-7, and a name that is not valid modified UTF-7 raises
``UnicodeDecodeError`` from inside a call that looks like it can only fail with
a protocol error. §7.1 is blunt about why it matters: an adapter that lets one
through has not decided whether the engine should retry, and the engine has no
way to decide for it. ``tests/test_imap_client.py`` asserts it rather than
trusting it.

**It does not retry either.** The engine already backs off with jitter (§7.3);
a second loop underneath it would multiply every wait by a number invisible
from the outside. Gmail's one exception — a 401 that means the access token
aged out — has no IMAP equivalent: a password does not expire mid-run, so a
refused login is refused.

**No ``MailTransientError`` raised here carries a ``retry_after``**, and that
is the protocol rather than an oversight. RFC 3501 has no ``Retry-After``: a
server that wants a client to slow down either answers ``NO`` with a sentence
in English or stops answering, and neither is a number. The field is documented
as a *floor* the engine may exceed and never undercut, so leaving it unset says
the true thing — this provider offered no floor — where inventing one would
override the engine's own backoff with a guess.

Two things about this connection that the HTTP adapters never have to think
about:

**It is stateful.** ``UID SEARCH`` and ``UID FETCH`` act on whatever folder was
last selected, so the folder is not an argument to a command — it is a property
of the connection. That is why :class:`ImapClient` selects once and remembers,
and why :meth:`ImapClient.select` exists as a way to *re-read* the folder's
state rather than as a way to change subject.

**It is single-threaded, and the engine is not.** ``ImportEngine`` runs
``fetch_concurrency`` fetch streams at once against one source object (§7.3,
eight by default), and IMAP has one socket with one command in flight. Without
the lock below, two coroutines would interleave a ``UID FETCH`` with a
``SELECT`` and read each other's untagged responses — not a slow client but a
wrong one, handing the archive another message's bytes. ``asyncio.Lock``
serialises the conversation while ``asyncio.to_thread`` keeps every blocking
call off the event loop, which is the pairing
:mod:`mailarc_core.mail.ports` names in its own docstring.
"""

import asyncio
import logging
import ssl
from collections.abc import Callable, Sequence
from typing import Any

from imapclient import IMAPClient, SocketTimeout
from imapclient.exceptions import (
    IMAPClientAbortError,
    IMAPClientError,
    LoginError,
)

from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_imap.source.config import ImapConfig
from mailarc_imap.source.credentials import ImapCredentials
from mailarc_imap.source.model import FetchedBody, FolderListing, FolderState

logger = logging.getLogger(__name__)

BODY_PEEK = b"BODY.PEEK[]"
"""Fetch the whole message **without** setting ``\\Seen``.

``RFC822`` and ``BODY[]`` fetch the same bytes and mark the message as read as
a side effect. This archive is reading somebody's real mailbox, often one they
share with nobody and have three thousand unread newsletters in; turning their
unread mail read is a visible, irreversible edit made by a program that was
asked only to copy. ``.PEEK`` is the whole difference and there is no reason to
ever spell it the other way.
"""

BODY_RESPONSE = b"BODY[]"
"""What the server answers ``BODY.PEEK[]`` with.

RFC 3501 §6.4.5: the ``.PEEK`` is an instruction to the server, not part of the
section name, so it does not come back. A client that looks for
``BODY.PEEK[]`` in the reply finds nothing and concludes every message is
missing.
"""

MESSAGE_SIZE = b"RFC822.SIZE"
"""The server's own size for a message, fetched alongside the bytes."""

SEARCH_UID = "UID"
"""The ``UID SEARCH`` key, spelled once."""


class ImapClient:
    """One authenticated connection to one mailbox, with one folder selected.

    Connects lazily: nothing dials a server until the first command, so
    building a source is free and a worker that claims a job and then finds it
    cancelled never opens a socket.
    """

    def __init__(
        self, credentials: ImapCredentials, config: ImapConfig | None = None
    ) -> None:
        self._credentials = credentials
        self._config = config or ImapConfig()
        self._lock = asyncio.Lock()
        self._connection: IMAPClient | None = None
        self._state: FolderState | None = None
        self._closed = False

    @property
    def credentials(self) -> ImapCredentials:
        """The credentials this connection speaks with. Nothing rotates them."""
        return self._credentials

    async def select(self, folder: str) -> FolderState:
        """``EXAMINE`` *folder* and read back ``UIDVALIDITY`` and ``UIDNEXT``.

        A round trip every time, deliberately. The two numbers it brings back
        are what the cursor is made of, and a cached ``UIDNEXT`` is a watermark
        that sits in front of mail that has since arrived — the one direction
        §7.4 says a watermark may never err in.
        """
        return await self._run(lambda: self._select(folder))

    async def list_folders(self) -> tuple[FolderListing, ...]:
        """Every mailbox the account can see, as the server names them.

        Names come back already decoded from modified UTF-7: ``IMAPClient``
        does it in ``_proc_folder_list`` whenever ``folder_encode`` is set,
        which it is by default and this adapter never clears — and it encodes
        again on the way in through ``_normalise_folder``. Verified against
        ``imapclient`` 3.1.0's source rather than assumed, because doing it a
        second time here would double-encode every umlaut in a German mailbox.
        """
        return await self._run(self._list_folders)

    async def search_from(self, folder: str, first_uid: int) -> tuple[int, ...]:
        """Every UID in *folder* at or above ``first_uid``, sorted.

        The filter is not belt and braces. RFC 3501 §9 defines ``n:*`` as *the
        range between n and the largest UID in the mailbox*, and a range is
        unordered — so on a mailbox whose highest UID is 3, ``UID SEARCH UID
        5:*`` legitimately answers ``3``. Every real server does this. Without
        the comparison below, a delta at the top of a quiet mailbox would keep
        handing the engine the last message forever, and the cursor would never
        move.
        """
        uids = await self._run(lambda: self._search(folder, first_uid))
        return tuple(sorted(uid for uid in uids if uid >= first_uid))

    async def fetch_body(self, folder: str, uidvalidity: int, uid: int) -> FetchedBody:
        """The RFC 5322 bytes of one message, without marking it read.

        Takes the folder and the generation it was listed under, and checks
        both **inside the lock**, because that is the only place the check can
        be true. The engine runs eight streams at once (§7.3) and all of them
        share this one socket: a caller that selected a folder and then called
        a bare ``fetch_body`` would be fetching from whichever folder the
        *other* seven streams left selected, and the bytes of some other
        message would be archived under this one's id. Passing the folder in
        and re-selecting under the lock makes that unrepresentable.

        One command per message rather than one per batch. A single ``UID
        FETCH 1:100`` would be nine fewer round trips and would also
        materialise a hundred whole messages — attachments included — before
        the first of them could be handed on, which is exactly the thing
        ``fetch_raw`` returns a stream to avoid. The round trips are on a
        socket that is already open and already authenticated, so each costs a
        fraction of what one of Gmail's HTTPS requests does.

        A UID that is not in the reply is a message deleted between the listing
        and now. The server does not say so — it simply answers ``OK`` with no
        untagged ``FETCH`` — and the meaning is the same as Gmail's 404 on a
        message: skip it, write it down, keep going.
        """
        return await self._run(lambda: self._fetch(folder, uidvalidity, uid))

    async def aclose(self) -> None:
        """Log out and drop the connection. Safe to call twice (§7.1).

        ``LOGOUT`` rather than a bare socket close: a server that is told the
        client is leaving frees the mailbox lock immediately, where an
        abandoned socket sits in its idle timeout — and a mailbox with a stale
        lock is one the next run cannot select. A logout that itself fails is
        logged and swallowed, because by the time this runs the import is over
        and there is nothing left to fail.
        """
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            connection, self._connection, self._state = self._connection, None, None
            if connection is None:
                return
            await asyncio.to_thread(_disconnect, connection)

    async def _run[T](self, command: Callable[[], T]) -> T:
        """One blocking IMAP command, off the loop and alone on the socket.

        The lock is held across the connect as well as the command, so eight
        streams starting at once log in once rather than eight times.
        """
        async with self._lock:
            if self._closed:
                raise MailPermanentError("this IMAP connection is already closed")
            return await asyncio.to_thread(lambda: self._blocking(command))

    def _blocking[T](self, command: Callable[[], T]) -> T:
        """Connect if needed, run the command, and translate whatever it raises.

        The single place the taxonomy is decided, which is why every method
        above goes through it and none of them touches ``imapclient``.
        """
        try:
            self._connect()
            return command()
        except LoginError as error:
            # A rejected username or password — *unless* the socket died while
            # LOGIN was in flight, which `_login_failure` is here to tell apart.
            # The socket goes either way: an unauthenticated connection can
            # serve no command, so keeping it open would hold a file descriptor
            # until whoever owns this client got around to closing it.
            self._forget()
            raise self._login_failure(error) from error
        except IMAPClientAbortError as error:
            # imaplib raises this for a connection that went away mid-command,
            # including a plain EOF. The same command on a fresh connection is
            # the thing most likely to work.
            self._forget()
            raise MailTransientError(
                f"{self._credentials.host} dropped the connection: {error}"
            ) from error
        except IMAPClientError as error:
            # A well-formed command the server answered NO or BAD to. The
            # connection is fine and the next command may well work, so this is
            # not transient; this one request never will, so it is permanent.
            raise MailPermanentError(
                f"{self._credentials.host} refused a command: {error}"
            ) from error
        except ssl.SSLError as error:
            # Before OSError: SSLError is a subclass of it, and the two want
            # different sentences. A handshake that fails against a host that
            # was reachable is a proxy, a captive portal or a wrong port, and
            # every one of those is worth trying again from a different place.
            self._forget()
            raise MailTransientError(
                f"TLS to {self._credentials.host} failed: {error}"
            ) from error
        except (TimeoutError, OSError) as error:
            # Every socket failure this adapter can meet is already one of
            # these two: since 3.10 `socket.timeout` *is* `TimeoutError` and
            # `socket.error` *is* `OSError`, and `socket.gaierror` — a host
            # that does not resolve, which is what a typo in the form looks
            # like — is a subclass of the latter.
            self._forget()
            raise MailTransientError(
                f"{self._credentials.host} is unreachable: {error}"
            ) from error
        except ValueError as error:
            # A reply this adapter could not read. `imapclient` decodes every
            # folder name from modified UTF-7, and a server that emits a name
            # which is not valid modified UTF-7 — a shift sequence holding a
            # partial character, `&&&` — makes that decode raise
            # `UnicodeDecodeError`, which is a `ValueError` and belongs to no
            # branch above. Left uncaught it would leave this module and reach
            # an engine that has no handler for it, which is the one thing
            # §7.1 forbids outright.
            #
            # Transient rather than permanent, for the reason
            # `GmailClient._payload` gives for a 200 that is not JSON: a stream
            # this client cannot parse is far more often a proxy or a half-read
            # socket than a server with a broken mailbox name, and the
            # permanent branch would file it as a skipped message that never
            # existed. The connection goes with it — part of a response may
            # already have been consumed, so its state is no longer known.
            self._forget()
            raise MailTransientError(
                f"{self._credentials.host} sent a reply this client could not "
                f"read: {error}"
            ) from error

    def _login_failure(self, error: LoginError) -> MailError:
        """Whether a refused ``LOGIN`` was the password or the network.

        ``imapclient`` 3.1.0's ``login`` wraps **every** ``IMAPClientError`` the
        command raises into a ``LoginError`` — and ``IMAPClientAbortError`` is
        one of those, because it is ``imaplib.IMAP4.abort``, a subclass of
        ``imaplib.IMAP4.error``. So a connection that dies mid-login arrives
        here spelling itself "the credentials were refused", and believing it
        costs the account: :class:`~mailarc_core.mail.errors.MailAuthError` is
        terminal for the job, the row goes to ``auth_error``, the schedule stops
        and the UI asks a human to re-enter a password that was never wrong. A
        dropped socket during one command is the definition of transient.

        Told apart by ``__context__`` rather than by reading the message:
        ``raise LoginError(str(e))`` inside an ``except`` block sets the
        original on the new exception, so the abort is still there to be found,
        while sniffing for "EOF" in an English sentence would break on the first
        server that phrases it differently.
        """
        if isinstance(error.__context__, IMAPClientAbortError):
            return MailTransientError(
                f"{self._credentials.host} dropped the connection while "
                f"logging in: {error}"
            )
        # A rejected username or password. No amount of retrying fixes it, and
        # the remedy is a human editing the account — which for a provider with
        # no consent runner is the whole of "re-consent".
        return MailAuthError(
            f"{self._credentials.host} refused the credentials: {error}"
        )

    def _connect(self) -> None:
        """Open the socket, secure it and log in. Blocks; called under the lock.

        It does **not** select a folder. A caller that only wants the folder
        list would pay a round trip for a mailbox it never reads, and
        :meth:`_selected` opens one for the two commands that need it.
        """
        if self._connection is not None:
            return
        credentials = self._credentials
        connection = IMAPClient(
            credentials.host,
            port=credentials.port,
            ssl=True,
            ssl_context=self._tls(),
            # Two deadlines rather than one: reaching the host is allowed
            # fifteen seconds, a command streaming an attachment back is
            # allowed two minutes. A bare float would be collapsed into the
            # same value for both, which would make one of the two wrong.
            # `imapclient` annotates this parameter `Optional[float]` and then,
            # one line into its own constructor, tests for a `SocketTimeout`
            # and wraps only a bare float into one — so the annotation is
            # narrower than both the code and the documentation.
            timeout=SocketTimeout(  # ty: ignore[invalid-argument-type]
                connect=self._config.connect_timeout,
                read=self._config.request_timeout,
            ),
        )
        self._connection = connection
        connection.login(credentials.username, credentials.password)
        logger.debug("Opened %s as %s", credentials.host, credentials.username)

    def _tls(self) -> ssl.SSLContext | None:
        """The trust store to verify the server against, or the platform's.

        ``None`` lets ``imapclient`` build the default context, which is what
        iCloud and Gmail want. A configured bundle is for a mail server behind
        a private certificate authority — and for this component's own tests,
        which serve a self-signed certificate on a loopback socket rather than
        skip verification, because a client that has never verified anything in
        a test is a client whose verification nobody has checked.
        """
        if not self._config.tls_ca_file:
            return None
        return ssl.create_default_context(cafile=self._config.tls_ca_file)

    def _select(self, folder: str) -> FolderState:
        """``EXAMINE`` *folder* — read-only, so nothing is flagged.

        ``readonly=True`` sends ``EXAMINE`` instead of ``SELECT``. Both open the
        folder; only ``EXAMINE`` promises the server that this client will not
        change a flag, which for an archive is a promise worth making at the
        protocol level rather than by remembering not to.
        """
        response = self._require_connection().select_folder(folder, readonly=True)
        state = FolderState(
            folder=folder,
            uidvalidity=_number(response, b"UIDVALIDITY"),
            uidnext=_number(response, b"UIDNEXT"),
            exists=_number(response, b"EXISTS", default=0),
        )
        self._state = state
        return state

    def _list_folders(self) -> tuple[FolderListing, ...]:
        rows = self._require_connection().list_folders()
        return tuple(
            FolderListing(
                name=str(name),
                flags=tuple(flag for flag in flags if isinstance(flag, bytes)),
                delimiter=delimiter.decode() if isinstance(delimiter, bytes) else None,
            )
            for flags, delimiter, name in rows
        )

    def _selected(self, folder: str) -> FolderState:
        """Make *folder* the selected one, and say what it looks like now.

        ``UID SEARCH`` and ``UID FETCH`` act on the selected folder, so they
        cannot run before one is. A round trip only when the folder actually
        changes: a page of two hundred UIDs is one folder, so a walk pays one
        ``EXAMINE`` per page and not one per message.

        Doing it here rather than in :meth:`_connect` keeps the login from
        spending a round trip a caller that only wants the folder list would
        never use.
        """
        if self._state is None or self._state.folder != folder:
            return self._select(folder)
        return self._state

    def _search(self, folder: str, first_uid: int) -> Sequence[int]:
        self._selected(folder)
        return self._require_connection().search([SEARCH_UID, f"{first_uid}:*"])

    def _fetch(self, folder: str, uidvalidity: int, uid: int) -> FetchedBody:
        state = self._selected(folder)
        if state.uidvalidity != uidvalidity:
            # The folder was renumbered between the listing and this fetch, so
            # this UID now belongs to a different message. Fetching anyway
            # would file some other message's bytes under this one's id — the
            # one failure in this component no later run could detect or
            # repair, because the ledger would record it as archived.
            raise MailPermanentError(
                f"{folder} was renumbered mid-run: UID {uid} was listed under "
                f"UIDVALIDITY {uidvalidity}, the folder is now at "
                f"{state.uidvalidity}"
            )
        response = self._require_connection().fetch([uid], [BODY_PEEK, MESSAGE_SIZE])
        entry = response.get(uid)
        if entry is None:
            # No untagged FETCH at all, and an OK afterwards: the message was
            # deleted between the listing and now. The server never says so
            # outright, and the meaning is Gmail's 404 on a message.
            raise MailPermanentError(
                f"UID {uid} is no longer in {folder} on {self._credentials.host}"
            )
        raw = entry.get(BODY_RESPONSE)
        if not isinstance(raw, bytes):
            # A server that lists a message and then answers BODY[] with NIL,
            # or with nothing at all. Asking again returns the same answer, so
            # it is the same decision as a message that is gone — but a
            # different sentence, because the two are worth telling apart in a
            # `mail_failed_messages` row.
            raise MailPermanentError(f"UID {uid} in {folder} came back without a body")
        size = entry.get(MESSAGE_SIZE)
        return FetchedBody(
            uid=uid, raw=raw, size=size if isinstance(size, int) else None
        )

    def _require_connection(self) -> IMAPClient:
        """The open connection. ``_connect`` ran first; this is what says so."""
        if self._connection is None:  # pragma: no cover - _blocking connects first
            raise MailTransientError(f"no connection to {self._credentials.host}")
        return self._connection

    def _forget(self) -> None:
        """Drop a connection that is no longer usable, so the next call redials.

        Only for the failures that are about the socket. A ``NO`` to one
        command leaves a perfectly good session behind, and throwing it away
        would turn one refused fetch into a fresh login for every message after
        it.
        """
        connection, self._connection, self._state = self._connection, None, None
        if connection is not None:
            _shutdown_quietly(connection)


def _number(response: dict[bytes, Any], key: bytes, default: int | None = None) -> int:
    """One integer out of a ``SELECT``/``EXAMINE`` response.

    ``UIDVALIDITY`` and ``UIDNEXT`` are required of every server that supports
    UIDs, and this adapter is built entirely on UIDs — so a folder that answers
    without them cannot be walked at all, and saying so here beats inventing a
    zero that would make every stored cursor look expired.
    """
    value = response.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if default is not None:
        return default
    raise MailPermanentError(
        f"the server did not report {key.decode()} for this folder"
    )


def _disconnect(connection: IMAPClient) -> None:
    """``LOGOUT``, and a socket close if that is refused. Never raises."""
    try:
        connection.logout()
        return
    except (OSError, IMAPClientError) as error:
        logger.debug("Logout was refused, closing the socket instead: %s", error)
    _shutdown_quietly(connection)


def _shutdown_quietly(connection: IMAPClient) -> None:
    """Close the socket, whatever state it is in. Never raises.

    Called from the error path, where the connection is already known to be
    broken: a second failure while letting go of it has nothing left to tell
    anybody and must not replace the error that is on its way up.
    """
    try:
        connection.shutdown()
    except (OSError, IMAPClientError) as error:
        logger.debug("Could not close the IMAP socket: %s", error)
