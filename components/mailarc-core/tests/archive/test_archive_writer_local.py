"""The idempotence contract, proved against a real FalkorDB.

Skipped unless `task tauri:vendor` has produced the runtime, so a checkout
without it still runs green. `test_archive_writer.py` proves the same claim
against a fake session; this file is what makes it a claim about a graph — the
node and edge counts here come from Cypher, not from a test double.

The server fixtures live in ``conftest.py`` and are shared with the reader's
local test; they are SESSION scoped and tear themselves down explicitly. A
function-scoped one would spawn a redis-server per test and leave every one of
them to be reaped at interpreter exit. Tests isolate themselves with unique
graph names, never with a fresh server.
"""

import pytest

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local

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


def test_a_standalone_message_now_costs_a_thread_of_its_own(config) -> None:
    """What the IMAP fix is paid for in, counted rather than estimated.

    A message that names no conversation — no provider thread id, no
    ``References``, no ``In-Reply-To`` — used to write no ``Thread`` at all.
    It now opens one keyed on its own Message-ID, so that the reply which
    names that id can join it. One node and one edge per standalone message,
    for as long as the archive stands.
    """
    _archive(
        config,
        source=SOURCE.model_copy(update={"provider_thread_id": None, "labels": ()}),
    )

    nodes, edges = _counts(config)

    # 1 message, 4 addresses, 1 thread, 1 attachment, 1 account — no labels.
    assert nodes == 8
    # from + 2 to + 1 cc + thread + attachment + account.
    assert edges == 7
    with client.session(config) as graph:
        [[key]] = graph.execute("MATCH (t:Thread) RETURN t.id").rows
    assert key == "7:m1@example.com"
