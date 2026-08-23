"""One in-process IMAP server per test, and the credentials that reach it.

Every fixture here points at ``127.0.0.1`` on a port the operating system
picked. Nothing in this component's suite may open a socket to a real mail
host, and nothing in it may touch the developer's archive — the root
``conftest.py`` seals ``.state``, and this file adds no path of its own.

The server speaks TLS and the client verifies it, because the adapter offers no
way to do anything else and a suite that skipped verification would be a suite
in which the adapter's certificate handling was never exercised. The
certificate is minted once per session (:mod:`tls`) — once, because generating
one per test is the kind of cost that turns a fast suite into a slow one for no
extra coverage.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from imap_server import FakeImapServer
from tls import LoopbackCertificate

from mailarc_imap.source import ImapConfig, ImapCredentials, ImapSource


@pytest.fixture(scope="session")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> LoopbackCertificate:
    """One throwaway certificate for the whole run, in a directory pytest owns."""
    directory: Path = tmp_path_factory.mktemp("imap-tls")
    return LoopbackCertificate(directory)


@pytest.fixture
async def server(certificate: LoopbackCertificate) -> AsyncIterator[FakeImapServer]:
    """A listening IMAP server with an empty ``INBOX``, stopped afterwards."""
    fake = FakeImapServer(certificate.server_context())
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def config(certificate: LoopbackCertificate) -> ImapConfig:
    """Short deadlines, the test certificate, and a page size the paging tests fill.

    The timeouts are deliberately small: a test that hangs for two minutes on a
    dropped socket is a test nobody runs.
    """
    return ImapConfig(
        connect_timeout=5.0,
        request_timeout=5.0,
        page_size=100,
        tls_ca_file=certificate.ca_file,
    )


@pytest.fixture
def credentials(server: FakeImapServer) -> ImapCredentials:
    """The account form's answer for the fake server, as a parsed credential."""
    return ImapCredentials(
        host="127.0.0.1",
        port=server.port,
        username=server.username,
        password=server.password,
    )


@pytest.fixture
async def source(
    credentials: ImapCredentials, config: ImapConfig
) -> AsyncIterator[ImapSource]:
    """A source against the fake server, closed however the test ends."""
    built = ImapSource(credentials, config)
    try:
        yield built
    finally:
        await built.aclose()
