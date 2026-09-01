"""A minimal IMAP4rev1 server on a loopback socket, for these tests alone.

**No test in this component may talk to a real IMAP server**, and a mock of
``imapclient`` would not be a test of this adapter — it would be a test of the
mock, and every wire-level fact the adapter depends on (``BODY.PEEK[]`` comes
back as ``BODY[]``, ``UID SEARCH UID 5:*`` answers with UID 3 on a mailbox
whose highest UID is 3) would be assumed rather than exercised. So the suite
runs a real server over a real socket and drives a real ``IMAPClient`` at it.

It speaks only the commands this adapter issues — ``CAPABILITY``, ``LOGIN``,
``LIST``, ``EXAMINE``/``SELECT``, ``UID SEARCH``, ``UID FETCH``, ``NOOP``,
``LOGOUT`` — and answers ``BAD`` to everything else, which is what a server
does and what keeps this file from growing into a mail server.

Deliberately **not** ``aioimaplib.imap_testing_server``: it is GPL-3.0, which
this MIT project with a distributed desktop bundle cannot link against even in
a test tree, and it needs ``pytz``, which is not installed.

It serves TLS, because the adapter offers no way to speak anything else — see
:mod:`tls` for the throwaway certificate it uses and why verification is left
switched on.

Knobs turn the failure paths on, because none of them is reachable without
unplugging a network cable or finding a broken server:
:attr:`FakeImapServer.reject_login` refuses the credentials,
:attr:`FakeImapServer.drop_after` closes the socket in the middle of the n-th
command, `FakeImapServer.omit_uidvalidity` answers ``EXAMINE`` without the one number
this adapter cannot walk a folder without,
:attr:`FakeImapServer.answer_without_body` lists a message it will not hand
over, :attr:`FakeImapServer.malformed_folder` puts a name on the wire that is
not valid modified UTF-7, and :attr:`FakeImapServer.refuse_logout` answers
``LOGOUT`` with ``NO`` — the one thing that can go wrong while a client is
letting go of a connection it has already finished with.

:attr:`FakeImapServer.reply_delay` is not a failure knob but a *detector*. A
server answers one command at a time, so the only way to see whether a client
sent a second one before the first was answered is to take a measurable moment
over the answer and watch the socket while doing it — see :meth:`_composing`.
"""

import asyncio
import contextlib
import logging
import re
import ssl
from collections import deque
from collections.abc import Iterator

from imapclient.imap_utf7 import decode as decode_utf7
from imapclient.imap_utf7 import encode as encode_utf7
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

MESSAGE = b"""\
From: Bob <bob@example.invalid>
To: Jens <jens@example.invalid>
Subject: A message that already exists
Message-ID: <%s@example.invalid>
Date: Mon, 3 Mar 2025 09:00:00 +0000

The body of message %s.
"""
"""One RFC 5322 message, parameterised so a mailbox can hold distinguishable ones."""

CRLF = b"\r\n"
GREETING = b"* OK [CAPABILITY IMAP4rev1] fake IMAP ready"
DELIMITER = "/"

OVERLAPPING = "!! OVERLAPPING COMMAND"
"""Recorded when a second command arrives before the first was answered.

IMAP is one socket with one command in flight, and this adapter runs several
fetch streams over one connection, so "did the client ever speak out of turn"
is the invariant its lock exists to keep. The marker goes into
:attr:`FakeImapServer.commands` where a test can look for its absence.
"""

NO_PEEK = "!! FETCH WITHOUT PEEK"
"""Recorded when a fetch arrives that would set ``\\Seen`` on somebody's mail."""

MALFORMED_FOLDER = "Gel&AO"
"""A ``LIST`` name that is not valid modified UTF-7.

``&AO`` opens a shift sequence and gives it twelve bits, which is not a
character — so ``imapclient``'s decode raises ``UnicodeDecodeError``, a
``ValueError``, and therefore the one kind of failure that slips past a handler
written for protocol and socket errors. (``Gel&APY`` would *not* do: an
unterminated sequence carrying a whole character decodes cleanly, so the
obvious truncation is not the one that breaks.) Servers really do emit names
like this; a mailbox created over another protocol with a raw eight-bit name is
the usual way.
"""

_TOKENS = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')
"""One IMAP argument: a quoted string, or a bare atom."""


def eml(marker: object) -> bytes:
    """A complete message whose bytes are unique to ``marker``."""
    token = str(marker).encode()
    return MESSAGE % (token, token)


class FakeMailbox(BaseModel):
    """One folder: its UID generation, its messages and its ``LIST`` flags."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    uidvalidity: int = 1000
    messages: dict[int, bytes] = Field(default_factory=dict)
    flags: tuple[bytes, ...] = (rb"\HasNoChildren",)

    @property
    def uids(self) -> list[int]:
        return sorted(self.messages)

    @property
    def uidnext(self) -> int:
        return (max(self.uids) + 1) if self.messages else 1

    def add(self, uid: int, raw: bytes) -> None:
        self.messages[uid] = raw


class FakeImapServer:
    """An IMAP4rev1 server bound to an ephemeral loopback port."""

    def __init__(
        self,
        context: ssl.SSLContext | None = None,
        *,
        username: str = "jens@example.invalid",
        password: str = "app-specific-password",  # noqa: S107 - a fixture
    ) -> None:
        self.context = context
        self.username = username
        self.password = password
        self.reject_login = False
        self.drop_after: int | None = None
        self.omit_uidvalidity = False
        self.answer_without_body = False
        self.malformed_folder = False
        self.refuse_logout = False
        self.reply_delay = 0.0
        self.folders: dict[str, FakeMailbox] = {"INBOX": FakeMailbox()}
        self.commands: list[str] = []
        self._open: set[asyncio.StreamWriter] = set()
        self._selected: FakeMailbox | None = None
        self._server: asyncio.Server | None = None
        self._commands_seen = 0

    @property
    def port(self) -> int:
        assert self._server is not None, "the server is not listening"
        return self._server.sockets[0].getsockname()[1]

    def mailbox(self, name: str = "INBOX") -> FakeMailbox:
        """The folder by that name, created empty if it is not there yet."""
        return self.folders.setdefault(name, FakeMailbox())

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._serve, "127.0.0.1", 0, ssl=self.context
        )

    async def stop(self) -> None:
        """Stop listening and let every open connection go.

        ``wait_closed`` waits for the handlers, so a client that was killed
        rather than logged out is given its connection back here rather than
        left for the garbage collector — which on Python 3.14 reaps a detached
        transport with a ``TypeError`` nobody can act on.
        """
        if self._server is None:
            return
        server, self._server = self._server, None
        for writer in list(self._open):
            writer.close()
        server.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=5)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._open.add(writer)
        # Anything read early by `_composing` while an answer was being
        # composed. Handled before the socket is read again, so noticing a
        # client that spoke out of turn does not also drop what it said.
        early: deque[bytes] = deque()
        try:
            writer.write(GREETING + CRLF)
            await writer.drain()
            while True:
                line = early.popleft() if early else await reader.readline()
                if not line:
                    return
                if await self._handle(line, writer, reader, early) is False:
                    return
        except OSError, ssl.SSLError:
            return
        finally:
            self._open.discard(writer)
            writer.close()
            with contextlib.suppress(OSError, ssl.SSLError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=5)

    async def _handle(
        self,
        line: bytes,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        early: deque[bytes],
    ) -> bool:
        """One command. ``False`` ends the connection."""
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        self.commands.append(text)
        self._commands_seen += 1
        if self.drop_after is not None and self._commands_seen >= self.drop_after:
            logger.debug("Dropping the connection on command %r", text)
            writer.close()
            return False
        tag, _, rest = text.partition(" ")
        name, _, arguments = rest.partition(" ")
        replies = list(self._reply(tag, name.upper(), _split(arguments)))
        await self._composing(reader, early)
        for reply in replies:
            writer.write(reply + CRLF)
        await writer.drain()
        return name.upper() != "LOGOUT"

    async def _composing(
        self, reader: asyncio.StreamReader, early: deque[bytes]
    ) -> None:
        """Take :attr:`reply_delay` over the answer, watching the socket meanwhile.

        The only way one connection can observe a client sending two commands
        at once. A server reads a line, answers it and reads the next, so by the
        time the second line is read the first has long been answered and
        nothing is left to see. Holding the answer back for a measurable moment
        and reading during it turns that into an observation: with the client's
        lock in place nothing arrives and this times out, and without it the
        next fetch is already on the wire.

        Anything read here is queued rather than dropped — the point is to
        notice the overlap, not to break the run that produced it.
        """
        if not self.reply_delay:
            return
        try:
            line = await asyncio.wait_for(reader.readline(), self.reply_delay)
        except TimeoutError:
            return
        if not line:
            return
        logger.debug("A second command arrived before the first was answered")
        self.commands.append(OVERLAPPING)
        early.append(line)

    def _reply(self, tag: str, name: str, args: list[str]) -> Iterator[bytes]:
        if name == "CAPABILITY":
            yield b"* CAPABILITY IMAP4rev1"
            yield f"{tag} OK CAPABILITY completed".encode()
        elif name == "LOGIN":
            yield from self._login(tag, args)
        elif name == "LIST":
            yield from self._list(tag)
        elif name in ("EXAMINE", "SELECT"):
            yield from self._examine(tag, name, args)
        elif name == "UID":
            yield from self._uid(tag, args)
        elif name == "NOOP":
            yield f"{tag} OK NOOP completed".encode()
        elif name == "LOGOUT":
            if self.refuse_logout:
                yield f"{tag} NO Cannot log out just now".encode()
                return
            yield b"* BYE fake IMAP signing off"
            yield f"{tag} OK LOGOUT completed".encode()
        else:
            yield f"{tag} BAD Unknown command {name}".encode()

    def _login(self, tag: str, args: list[str]) -> Iterator[bytes]:
        given = [*args, "", ""][:2]
        if self.reject_login or given != [self.username, self.password]:
            yield f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials".encode()
            return
        yield f"{tag} OK LOGIN completed".encode()

    def _list(self, tag: str) -> Iterator[bytes]:
        for name, folder in self.folders.items():
            flags = b" ".join(folder.flags).decode()
            yield f'* LIST ({flags}) "{DELIMITER}" "{_encoded(name)}"'.encode()
        if self.malformed_folder:
            # Verbatim, not through `_encoded`: the whole point is a name the
            # client's own decoder refuses. See :data:`MALFORMED_FOLDER`.
            yield f'* LIST (\\HasNoChildren) "{DELIMITER}" "{MALFORMED_FOLDER}"'.encode()
        yield f"{tag} OK LIST completed".encode()

    def _examine(self, tag: str, name: str, args: list[str]) -> Iterator[bytes]:
        wanted = _decoded(args[0]) if args else ""
        folder = self.folders.get(wanted)
        if folder is None:
            yield f"{tag} NO [NONEXISTENT] Mailbox does not exist".encode()
            return
        self._selected = folder
        yield rb"* FLAGS (\Seen \Answered \Flagged)"
        yield f"* {len(folder.messages)} EXISTS".encode()
        yield b"* 0 RECENT"
        if not self.omit_uidvalidity:
            yield f"* OK [UIDVALIDITY {folder.uidvalidity}] UIDs valid".encode()
        yield f"* OK [UIDNEXT {folder.uidnext}] Predicted next UID".encode()
        yield f"{tag} OK [READ-ONLY] {name} completed".encode()

    def _uid(self, tag: str, args: list[str]) -> Iterator[bytes]:
        if not args:
            yield f"{tag} BAD UID needs a command".encode()
            return
        sub, rest = args[0].upper(), args[1:]
        folder = self._selected
        if folder is None:
            yield f"{tag} BAD No mailbox selected".encode()
            return
        if sub == "SEARCH":
            yield from self._search(tag, folder, rest)
        elif sub == "FETCH":
            yield from self._fetch(tag, folder, rest)
        else:
            yield f"{tag} BAD Unknown UID command {sub}".encode()

    def _search(
        self, tag: str, folder: FakeMailbox, args: list[str]
    ) -> Iterator[bytes]:
        """``UID SEARCH UID a:b``, with RFC 3501 §9's range semantics.

        ``a:*`` is *the range between a and the highest UID in the mailbox*,
        and a range is unordered — so ``5:*`` on a mailbox whose highest UID is
        3 matches UID 3, not nothing. Every real server behaves this way and
        the adapter's own filter is the thing being tested, so the fake must
        behave this way too.
        """
        if len(args) < 2 or args[0].upper() != "UID":
            yield f"{tag} BAD Unsupported search criteria".encode()
            return
        low, high = _range(args[1], folder)
        matched = [uid for uid in folder.uids if low <= uid <= high]
        yield ("* SEARCH " + " ".join(str(uid) for uid in matched)).rstrip().encode()
        yield f"{tag} OK SEARCH completed".encode()

    def _fetch(self, tag: str, folder: FakeMailbox, args: list[str]) -> Iterator[bytes]:
        """``UID FETCH set (BODY.PEEK[] RFC822.SIZE)``.

        The literal has to be the **last** item on the line: imaplib reads one
        only when a line ends in ``{n}``. And ``BODY.PEEK[]`` is answered as
        ``BODY[]`` — RFC 3501 §6.4.5, the ``.PEEK`` is an instruction and not
        part of the section name — which is the fact the adapter's
        ``BODY_RESPONSE`` constant exists for.
        """
        if not args:
            yield f"{tag} BAD FETCH needs a message set".encode()
            return
        peeking = "PEEK" in " ".join(args).upper()
        for uid in _requested(args[0], folder):
            raw = folder.messages[uid]
            sequence = folder.uids.index(uid) + 1
            if self.answer_without_body:
                yield f"* {sequence} FETCH (UID {uid} RFC822.SIZE {len(raw)})".encode()
                continue
            head = f"* {sequence} FETCH (UID {uid} RFC822.SIZE {len(raw)} BODY[] "
            yield head.encode() + b"{" + str(len(raw)).encode() + b"}"
            yield raw + b")"
        if not peeking:
            # Nothing in this adapter should ever get here. The fake records it
            # so a test can assert that no unread mail was touched.
            self.commands.append(NO_PEEK)
        yield f"{tag} OK FETCH completed".encode()


def _requested(message_set: str, folder: FakeMailbox) -> list[int]:
    """The UIDs of a fetch set that actually exist. A missing one is simply absent."""
    wanted: list[int] = []
    for part in message_set.split(","):
        low, high = _range(part, folder)
        wanted.extend(uid for uid in folder.uids if low <= uid <= high)
    return wanted


def _range(part: str, folder: FakeMailbox) -> tuple[int, int]:
    """One element of a sequence set as an inclusive, ordered pair."""
    low, _, high = part.partition(":")
    highest = max(folder.uids) if folder.messages else 0
    first = highest if low == "*" else int(low)
    last = highest if high in ("*", "") else int(high)
    return min(first, last), max(first, last)


def _split(arguments: str) -> list[str]:
    """An IMAP argument line into its atoms, unquoting as it goes."""
    return [
        (quoted if quoted else atom).replace('\\"', '"').replace("\\\\", "\\")
        for quoted, atom in _TOKENS.findall(arguments)
    ]


def _encoded(name: str) -> str:
    """A folder name as the wire carries it: modified UTF-7.

    Only the client is supposed to decode this, which is the point — a test
    with a German folder name proves the adapter does not decode it twice.
    """
    return encode_utf7(name).decode("ascii")


def _decoded(name: str) -> str:
    """The inverse of :func:`_encoded`, for the folder a client asks to select."""
    return decode_utf7(name.encode("ascii"))
