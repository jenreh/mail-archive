"""The conversation, and the promise that nothing protocol-shaped escapes it.

`client.py` is the only module in this component allowed to import
``imapclient``, and the only one that may read what a server said. Everything
here is about the boundary that makes that true: a login refused, a socket that
went away, a handshake against a server that is not speaking TLS, a UID that is
no longer there. Each one has exactly one right answer in
:mod:`mailarc_core.mail.errors`, and the engine cannot pick it for the adapter.

The last class is the blunt version of the rule: it imports every module in the
component and asserts that a call which fails does not fail with something the
engine has never heard of.
"""

import ast
import asyncio
import contextlib
import inspect
import ssl
from collections.abc import AsyncIterator
from types import ModuleType

import pytest
from imap_server import FakeImapServer, eml
from imapclient.exceptions import IMAPClientError

from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)
from mailarc_imap.source import (
    ImapClient,
    ImapConfig,
    ImapCredentials,
    config,
    credentials,
    mapping,
    model,
    source,
)
from mailarc_imap.source.client import BODY_PEEK, BODY_RESPONSE

INBOX = "INBOX"
"""The folder every test here works in; the fake server serves it by default."""


@pytest.fixture
async def client(
    credentials: ImapCredentials, config: ImapConfig
) -> AsyncIterator[ImapClient]:
    """A client against the fake server, closed however the test ends."""
    built = ImapClient(credentials, config)
    try:
        yield built
    finally:
        await built.aclose()


class TestTheHappyPath:
    """One connection, one selected folder, and the numbers the cursor is made of."""

    async def test_selecting_reports_the_folders_generation(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        server.mailbox().uidvalidity = 4711
        server.mailbox().add(7, eml(7))

        state = await client.select(INBOX)

        assert state.uidvalidity == 4711
        assert state.uidnext == 8
        assert state.exists == 1

    async def test_it_examines_rather_than_selects(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """``EXAMINE`` is the protocol-level promise that no flag will change."""
        await client.select(INBOX)

        assert any("EXAMINE" in command for command in server.commands)
        assert not any("SELECT" in command for command in server.commands)

    async def test_it_connects_only_once_for_many_commands(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        await client.select(INBOX)
        await client.select(INBOX)
        await client.list_folders()

        assert sum("LOGIN" in command for command in server.commands) == 1

    async def test_nothing_is_dialled_until_the_first_command(
        self, credentials: ImapCredentials, config: ImapConfig, server: FakeImapServer
    ) -> None:
        ImapClient(credentials, config)
        await asyncio.sleep(0)

        assert server.commands == []

    async def test_folder_names_come_back_decoded_once(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """``imapclient`` decodes modified UTF-7; doing it again would mangle it."""
        server.mailbox("Gel\N{LATIN SMALL LETTER O WITH DIAERESIS}schtes")

        names = {listing.name for listing in await client.list_folders()}

        assert "Gel\N{LATIN SMALL LETTER O WITH DIAERESIS}schtes" in names

    async def test_a_search_answers_with_uids(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        for uid in (3, 9, 14):
            server.mailbox().add(uid, eml(uid))
        await client.select(INBOX)

        assert await client.search_from(INBOX, 1) == (3, 9, 14)

    async def test_a_fetch_brings_back_the_bytes_and_the_size(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        server.mailbox().add(5, eml(5))
        await client.select(INBOX)

        body = await client.fetch_body(INBOX, server.mailbox().uidvalidity, 5)

        assert body.raw == eml(5)
        assert body.size == len(eml(5))

    async def test_a_fetch_opens_the_folder_if_nothing_has_yet(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """``UID FETCH`` acts on the selected folder, so it cannot run before one is.

        Nothing in :class:`~mailarc_imap.source.source.ImapSource` reaches here
        — every listing selects, and ``fetch_raw`` asks for the folder state
        first — but the client is a class of its own with its own contract, and
        the alternative to opening the folder is a ``BAD No mailbox selected``.
        """
        server.mailbox().add(3, eml(3))

        body = await client.fetch_body(INBOX, server.mailbox().uidvalidity, 3)

        assert body.raw == eml(3)

    async def test_a_second_command_on_the_same_folder_does_not_re_examine(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """Eight fetch streams share one socket; re-examining per message would
        cost a round trip each."""
        server.mailbox().add(1, eml(1))
        await client.select(INBOX)
        before = len([c for c in server.commands if "EXAMINE" in c])

        await client.search_from(INBOX, 1)
        await client.fetch_body(INBOX, server.mailbox().uidvalidity, 1)

        after = len([c for c in server.commands if "EXAMINE" in c])
        assert after == before

    async def test_naming_another_folder_re_examines(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """The correctness half of the same mechanism.

        The engine runs eight streams over one socket, so a fetch that trusted
        whichever folder happened to be selected would hand back another
        folder's message under this one's id.
        """
        server.mailbox("Reisen").add(4, eml(4))
        await client.select(INBOX)
        before = len([c for c in server.commands if "EXAMINE" in c])

        body = await client.fetch_body(
            "Reisen", server.mailbox("Reisen").uidvalidity, 4
        )

        assert body.raw == eml(4)
        assert len([c for c in server.commands if "EXAMINE" in c]) == before + 1

    async def test_a_uid_listed_under_another_generation_is_permanent(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """A renumbered folder mid-run: fetching anyway would archive some other
        message's bytes under this one's id, which no later run could repair."""
        server.mailbox().add(1, eml(1))

        with pytest.raises(MailPermanentError, match="renumbered mid-run"):
            await client.fetch_body(INBOX, server.mailbox().uidvalidity + 1, 1)

    async def test_a_folder_with_no_uidvalidity_cannot_be_walked(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """Every UID this adapter stores is meaningless without it."""
        server.omit_uidvalidity = True

        with pytest.raises(MailPermanentError, match="did not report UIDVALIDITY"):
            await client.select(INBOX)


class TestTheUnreadMailIsNotTouched:
    """Marking a stranger's unread mail as read is a user-visible defect."""

    async def test_the_fetch_asks_for_a_peek(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        server.mailbox().add(1, eml(1))
        await client.select(INBOX)
        await client.fetch_body(INBOX, server.mailbox().uidvalidity, 1)

        fetches = [c for c in server.commands if "FETCH" in c]
        assert fetches
        assert all(BODY_PEEK.decode() in command for command in fetches)
        assert "!! FETCH WITHOUT PEEK" not in server.commands

    def test_the_reply_is_read_under_the_name_the_server_answers_with(self) -> None:
        """RFC 3501 §6.4.5: the ``.PEEK`` is an instruction, not a section name."""
        assert BODY_PEEK == b"BODY.PEEK[]"
        assert BODY_RESPONSE == b"BODY[]"


class TestTheSearchRangeQuirk:
    """``n:*`` is a range, and a range is unordered — RFC 3501 §9."""

    async def test_a_server_answers_the_last_uid_for_a_range_above_it(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """The fake reproduces what every real server does. Read the next test."""
        server.mailbox().add(3, eml(3))
        await client.select(INBOX)

        await client.search_from(INBOX, 5)

        assert any("UID SEARCH UID 5:*" in command for command in server.commands)

    async def test_the_client_filters_it_out(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """Without this, a delta at the top of a quiet mailbox never advances."""
        server.mailbox().add(3, eml(3))
        await client.select(INBOX)

        assert await client.search_from(INBOX, 5) == ()

    async def test_results_come_back_sorted(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        for uid in (12, 2, 7):
            server.mailbox().add(uid, eml(uid))
        await client.select(INBOX)

        assert await client.search_from(INBOX, 1) == (2, 7, 12)


class TestTheErrorTaxonomy:
    """Four answers, and the status of the connection picks between them."""

    async def test_a_refused_login_is_an_auth_error(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        server.reject_login = True

        with pytest.raises(MailAuthError):
            await client.select(INBOX)

    async def test_a_wrong_password_is_an_auth_error(
        self, config: ImapConfig, server: FakeImapServer
    ) -> None:
        wrong = ImapCredentials(
            host="127.0.0.1",
            port=server.port,
            username=server.username,
            password="not-the-app-password",
        )
        built = ImapClient(wrong, config)

        with pytest.raises(MailAuthError):
            await built.select(INBOX)
        await built.aclose()

    async def test_a_dropped_connection_is_transient(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """Login, then nothing: the same command on a new socket may well work."""
        server.drop_after = 3

        with pytest.raises(MailTransientError):
            await client.select(INBOX)

    async def test_a_connection_dropped_during_login_is_transient_too(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """The one place ``imapclient`` makes a network failure look like a password.

        ``IMAPClient.login`` wraps every ``IMAPClientError`` the command raises
        into a ``LoginError``, and the abort ``imaplib`` raises for an EOF
        mid-command is one — so a socket that dies while ``LOGIN`` is in flight
        arrives spelling itself "the credentials were refused". Believing that
        sends the account to ``auth_error``, stops its schedule and asks a human
        to re-enter a password that was never wrong.

        The second command is ``LOGIN``: ``CAPABILITY`` is the first.
        """
        server.drop_after = 2

        with pytest.raises(MailTransientError) as raised:
            await client.select(INBOX)

        assert not isinstance(raised.value, MailAuthError)
        assert "logging in" in str(raised.value)

    async def test_a_reply_that_cannot_be_decoded_stays_in_the_taxonomy(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """``imapclient`` decodes folder names, and a decode raises a ``ValueError``.

        ``UnicodeDecodeError`` is neither an ``IMAPClientError`` nor an
        ``OSError``, so a handler written for protocol and socket failures lets
        it straight through to an engine that has no branch for it — the one
        thing the port forbids outright.
        """
        server.malformed_folder = True

        with pytest.raises(MailTransientError, match="could not read"):
            await client.list_folders()

    async def test_a_reply_that_cannot_be_decoded_is_not_a_bare_value_error(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """The proof that the branch above is the one being exercised."""
        server.malformed_folder = True

        with pytest.raises(MailError) as raised:
            await client.list_folders()

        assert isinstance(raised.value.__cause__, UnicodeDecodeError)

    async def test_an_unreachable_host_is_transient(self, config: ImapConfig) -> None:
        nowhere = ImapCredentials(
            host="127.0.0.1", port=1, username="jens", password="secret"
        )
        built = ImapClient(nowhere, config)

        with pytest.raises(MailTransientError):
            await built.select(INBOX)
        await built.aclose()

    async def test_a_timeout_is_transient(self, config: ImapConfig) -> None:
        """A socket that accepts and then says nothing at all."""
        listener = await asyncio.start_server(
            lambda reader, writer: asyncio.sleep(30), "127.0.0.1", 0
        )
        port = listener.sockets[0].getsockname()[1]
        built = ImapClient(
            ImapCredentials(
                host="127.0.0.1", port=port, username="jens", password="secret"
            ),
            config.model_copy(update={"connect_timeout": 0.4, "request_timeout": 0.4}),
        )

        try:
            with pytest.raises(MailTransientError):
                await built.select(INBOX)
        finally:
            await built.aclose()
            listener.close()
            await listener.wait_closed()

    async def test_a_server_that_is_not_speaking_tls_is_transient(
        self, config: ImapConfig
    ) -> None:
        """A captive portal, a proxy, or port 143 typed into the form."""
        plaintext = await asyncio.start_server(_greet_in_the_clear, "127.0.0.1", 0)
        port = plaintext.sockets[0].getsockname()[1]
        built = ImapClient(
            ImapCredentials(
                host="127.0.0.1", port=port, username="jens", password="secret"
            ),
            config,
        )

        try:
            with pytest.raises(MailTransientError) as raised:
                await built.select(INBOX)
            assert isinstance(raised.value.__cause__, ssl.SSLError)
        finally:
            await built.aclose()
            plaintext.close()
            await plaintext.wait_closed()

    async def test_a_certificate_the_client_does_not_trust_is_transient(
        self, server: FakeImapServer, credentials: ImapCredentials
    ) -> None:
        """The default trust store has never heard of this test certificate."""
        built = ImapClient(
            credentials, ImapConfig(connect_timeout=5.0, request_timeout=5.0)
        )

        with pytest.raises(MailTransientError) as raised:
            await built.select(INBOX)
        await built.aclose()

        assert isinstance(raised.value.__cause__, ssl.SSLError)

    async def test_a_folder_that_is_not_there_is_permanent(
        self, config: ImapConfig, server: FakeImapServer
    ) -> None:
        """A ``NO`` to a well-formed command: the session is fine, this call is not."""
        built = ImapClient(
            ImapCredentials(
                host="127.0.0.1",
                port=server.port,
                username=server.username,
                password=server.password,
            ),
            config,
        )

        with pytest.raises(MailPermanentError):
            await built.select("No Such Folder")
        await built.aclose()

    async def test_a_message_the_server_will_not_hand_over_is_permanent(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """Listed, fetched, and answered with no body. Asking again changes nothing."""
        server.mailbox().add(4, eml(4))
        server.answer_without_body = True
        await client.select(INBOX)

        with pytest.raises(MailPermanentError, match="came back without a body"):
            await client.fetch_body(INBOX, server.mailbox().uidvalidity, 4)

    async def test_no_transient_error_invents_a_retry_after(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """RFC 3501 has no ``Retry-After``, so this adapter must not claim one.

        ``retry_after`` is documented as a floor the engine may exceed and never
        undercut. A number invented here would silently override the engine's
        own backoff with a guess, and the honest answer — this provider offered
        no floor — is the field left unset.
        """
        server.drop_after = 3

        with pytest.raises(MailTransientError) as raised:
            await client.select(INBOX)

        assert raised.value.retry_after is None

    async def test_a_uid_that_vanished_is_permanent(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """Listed a moment ago, deleted since. The server simply answers nothing."""
        server.mailbox().add(4, eml(4))
        await client.select(INBOX)
        del server.mailbox().messages[4]

        with pytest.raises(MailPermanentError, match="no longer in INBOX"):
            await client.fetch_body(INBOX, server.mailbox().uidvalidity, 4)


class TestClosing:
    """``aclose`` is called from a ``finally`` the engine may reach twice."""

    async def test_it_logs_out(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        await client.select(INBOX)
        await client.aclose()

        assert any("LOGOUT" in command for command in server.commands)

    async def test_twice_is_safe(self, client: ImapClient) -> None:
        await client.select(INBOX)
        await client.aclose()
        await client.aclose()

    async def test_closing_something_never_opened_is_safe(
        self, credentials: ImapCredentials, config: ImapConfig
    ) -> None:
        await ImapClient(credentials, config).aclose()

    async def test_a_refused_logout_still_lets_the_connection_go(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        """``aclose`` runs from a ``finally`` and may not raise out of one.

        By the time it runs the import is over and there is nothing left to
        fail — but the socket still has to be released, or a worker that ran a
        thousand jobs holds a thousand file descriptors.
        """
        server.refuse_logout = True
        await client.select(INBOX)

        await client.aclose()

        with pytest.raises(MailPermanentError, match="already closed"):
            await client.select(INBOX)

    async def test_a_closed_client_does_not_silently_reconnect(
        self, client: ImapClient, server: FakeImapServer
    ) -> None:
        await client.select(INBOX)
        await client.aclose()
        before = len(server.commands)

        with pytest.raises(MailPermanentError, match="already closed"):
            await client.select(INBOX)

        assert len(server.commands) == before


class TestNothingProtocolShapedEscapes:
    """§7.1, asserted rather than trusted."""

    FORBIDDEN = (IMAPClientError, OSError, ssl.SSLError)

    FORBIDDEN_TOO = (ValueError,)
    """Separate from :attr:`FORBIDDEN` because it is not a protocol type at all.

    ``UnicodeDecodeError`` and every ``pydantic`` complaint are ``ValueError``s,
    and a handler written around ``imapclient`` catches neither. No
    :class:`~mailarc_core.mail.errors.MailError` is a ``ValueError``, so the
    assertion below is a real one rather than a tautology.
    """

    @pytest.mark.parametrize(
        "break_it",
        [
            pytest.param("reject_login", id="a refused login"),
            pytest.param("drop", id="a dropped connection"),
            pytest.param("malformed_folder", id="an undecodable folder name"),
        ],
    )
    async def test_every_failure_arrives_as_a_mail_error(
        self, client: ImapClient, server: FakeImapServer, break_it: str
    ) -> None:
        if break_it == "reject_login":
            server.reject_login = True
        elif break_it == "malformed_folder":
            server.malformed_folder = True
        else:
            server.drop_after = 2

        with pytest.raises(MailError) as raised:
            await client.list_folders()

        assert not isinstance(raised.value, self.FORBIDDEN)
        assert not isinstance(raised.value, self.FORBIDDEN_TOO)

    @pytest.mark.parametrize(
        "module",
        [model, config, credentials, mapping, source],
        ids=lambda module: module.__name__.rsplit(".", 1)[-1],
    )
    def test_only_the_client_module_imports_the_protocol_library(
        self, module: ModuleType
    ) -> None:
        """Everything else speaks the domain, so the seam is where it is claimed.

        Read off the parsed module rather than off its text, so a mention in a
        docstring does not count and an import inside a function does — the
        only two ways this check could be wrong in either direction.
        """
        tree = ast.parse(inspect.getsource(module))
        imported = {
            name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in _imported_from(node)
        }

        assert not imported & {"imapclient", "imaplib", "socket", "ssl"}


async def _greet_in_the_clear(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """An IMAP greeting with no TLS under it, which is what a wrong port answers."""
    writer.write(b"* OK plaintext IMAP\r\n")
    with contextlib.suppress(OSError):
        await writer.drain()
        await reader.read(1)
    writer.close()


def _imported_from(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Every top-level package one import statement pulls in."""
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.module else []
    return [alias.name for alias in node.names]
