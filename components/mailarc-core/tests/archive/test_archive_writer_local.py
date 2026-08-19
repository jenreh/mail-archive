"""The idempotence contract, proved against a real FalkorDB.

Skipped unless `task tauri:vendor` has produced the runtime, so a checkout
without it still runs green. `test_archive_writer.py` proves the same claim
against a fake session; this file is what makes it a claim about a graph — the
node and edge counts here come from Cypher, not from a test double.

The server fixture is SESSION scoped and tears itself down explicitly. A
function-scoped one would spawn a redis-server per test and leave every one of
them to be reaped at interpreter exit. Tests isolate themselves with unique
graph names, never with a fresh server.
"""

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR
from mailarc_core.graph.server import FalkorDBServer
from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()

EML = b"""\
From: Anna Bauer <anna@example.com>
To: Bob Baker <bob@example.com>, carl@example.com
Cc: dora@partner.example
Subject: [PROJ-123] Angebot Q3
Date: Wed, 04 Mar 2026 09:15:00 +0100
Message-ID: <m1@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset="utf-8"

Hallo Bob,

anbei das Angebot fuer Q3.

Viele Gruesse
Anna

--BOUND
Content-Type: application/pdf; name="Angebot.pdf"
Content-Disposition: attachment; filename="Angebot.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK

--BOUND--
"""

SOURCE = ArchiveSource(
    account_id="7",
    account_address="anna@example.com",
    provider=MailProvider.GMAIL,
    provider_message_id="g-1",
    provider_thread_id="t-1",
    folder="INBOX",
    labels=(
        LabelInfo(provider_label_id="INBOX", name="INBOX", kind=LabelKind.SYSTEM),
        LabelInfo(provider_label_id="Label_9", name="Kunden", kind=LabelKind.USER),
    ),
)

_issued_ports: set[int] = set()


def _free_port() -> int:
    """A port nothing is listening on, and never the same one twice.

    Its own range, well clear of `test_archive_writer_local`'s neighbours, so
    two session-scoped servers in one run cannot pick the same number before
    either of them has bound it.
    """
    for candidate in range(6600, 6700):
        if candidate in _issued_ports:
            continue
        with socket.socket() as sock:
            sock.settimeout(0.05)
            if sock.connect_ex(("127.0.0.1", candidate)) == 0:
                continue  # something is already listening
        _issued_ports.add(candidate)
        return candidate
    raise RuntimeError("no free port in 6600-6700 for the test FalkorDB")


@pytest.fixture(scope="session", autouse=True)
def _require_runtime() -> None:
    if not (RUNTIME_DIR / "falkordb.so").is_file():
        pytest.skip(
            f"vendored FalkorDB runtime not present at {RUNTIME_DIR}",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
def endpoint(tmp_path_factory) -> Iterator[GraphConfig]:
    """One server for the module, torn down explicitly rather than at exit."""
    config = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        graph_name="archive-probe",
        data_dir=tmp_path_factory.mktemp("archive-falkordb"),
        runtime_dir=RUNTIME_DIR,
        startup_timeout=30.0,
    )
    server = FalkorDBServer(config)
    server.start()
    try:
        yield config
    finally:
        server.stop()


@pytest.fixture
def config(endpoint: GraphConfig, request) -> GraphConfig:
    """A graph of this test's own, so counts start from an empty one."""
    return endpoint.model_copy(update={"graph_name": f"archive-{request.node.name}"})


def _counts(config: GraphConfig) -> tuple[int, int]:
    with client.session(config) as graph:
        nodes = graph.execute("MATCH (n) RETURN count(n)").rows[0][0]
        edges = graph.execute("MATCH ()-[r]->() RETURN count(r)").rows[0][0]
    return int(nodes), int(edges)


def _archive(config: GraphConfig, raw: bytes = EML, source=SOURCE) -> None:
    archiver = MessageArchiver(ArchiveConfig())
    with client.session(config) as graph:
        archiver.archive(graph, parse_message(raw), source)


def test_archiving_the_same_eml_twice_changes_nothing(config) -> None:
    """The Phase 1 contract, counted in the graph itself."""
    _archive(config)
    after_first = _counts(config)

    _archive(config)

    assert _counts(config) == after_first


def test_the_first_import_writes_the_nodes_the_message_names(config) -> None:
    """A count that never moves would also satisfy the test above."""
    _archive(config)

    nodes, edges = _counts(config)

    # 1 message, 4 addresses, 1 thread, 2 labels, 1 attachment, 1 account.
    assert nodes == 10
    # from + 2 to + 1 cc + thread + 2 labels + attachment + account.
    assert edges == 9


def test_the_message_comes_back_out_with_its_analysis_fields(config) -> None:
    """The round trip is the point: what went in has to be readable again."""
    _archive(config)

    with client.session(config) as graph:
        row = graph.execute(
            "MATCH (m:Message) "
            "RETURN m.id, m.subject_norm, m.participant_key, m.sent_at, m.simhash"
        ).rows[0]

    canonical_id, subject_norm, participant_key, sent_at, simhash = row
    assert canonical_id == "m1@example.com"
    assert subject_norm == "angebot q3"
    assert participant_key
    assert sent_at.startswith("2026-03-04")
    assert isinstance(simhash, int)


def test_the_same_mail_through_a_second_account_reuses_the_message(config) -> None:
    """One Message node, two ARCHIVED_FROM edges — the provenance rule."""
    _archive(config)
    nodes_before, edges_before = _counts(config)

    _archive(
        config,
        source=SOURCE.model_copy(
            update={
                "account_id": "8",
                "account_address": "anna@work.example",
                "provider": MailProvider.IMAP,
                "provider_message_id": "imap-9",
                "labels": (),
            }
        ),
    )

    nodes_after, edges_after = _counts(config)
    # The second Account, and the second account's own view of the thread —
    # a Thread is scoped to the account, a Message is not.
    assert nodes_after == nodes_before + 2
    assert edges_after == edges_before + 2  # ARCHIVED_FROM and IN_THREAD
