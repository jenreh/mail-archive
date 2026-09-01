"""Clearing one mailbox, proved against a real FalkorDB.

Skipped unless `task tauri:vendor` has produced the runtime, so a checkout
without it still runs green. ``test_archive_purge.py`` proves the same claims
against a fake session; this file is what makes them claims about a *graph* —
every count below comes from Cypher, not from a class in the test.

The claim worth the server is the one a fake cannot really make: the same mail
in two mailboxes is one node with two ``ARCHIVED_FROM`` edges, and clearing one
of them has to leave the other mailbox's copy readable, sender and attachment
and all. A ``DETACH DELETE`` over the wrong pattern passes every unit test in
the file next door and takes somebody else's mail with it here.

The server fixtures live in ``conftest.py`` and are shared with the writer's
and the reader's local tests: SESSION scoped and torn down explicitly, with
each test isolating itself through a graph name of its own.
"""

import pytest

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource
from mailarc_core.archive.purge import (
    DELETE_BATCH,
    PAGE_SIZE,
    PurgeCounts,
    purge_account,
)
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local

MINE = "7"
"""The mailbox under test — the ``Account`` node key, as the writer spells it."""

THEIRS = "8"
"""The second mailbox, whose mail a clear-out of the first may not touch."""


def _eml(message_id: str, subject: str = "Angebot Q3") -> bytes:
    """One message, distinguished by the id its canonical id is derived from."""
    return f"""\
From: Anna Bauer <anna@example.com>
To: Bob Baker <bob@example.com>
Cc: dora@partner.example
Subject: [PROJ-123] {subject}
Date: Wed, 04 Mar 2026 09:15:00 +0100
Message-ID: <{message_id}>
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
""".encode()


def _source(account: str, message_id: str) -> ArchiveSource:
    """The provenance of one copy, for one mailbox."""
    return ArchiveSource(
        account_id=account,
        account_address=f"anna{account}@example.com",
        provider=MailProvider.GMAIL,
        provider_message_id=f"g-{account}-{message_id}",
        provider_thread_id=f"t-{account}",
        folder="INBOX",
        labels=(
            LabelInfo(provider_label_id="INBOX", name="INBOX", kind=LabelKind.SYSTEM),
        ),
    )


def _archive(config: GraphConfig, account: str, *message_ids: str) -> None:
    """Import these messages into this mailbox."""
    archiver = MessageArchiver(ArchiveConfig())
    with client.session(config) as graph:
        for one in message_ids:
            archiver.archive(graph, parse_message(_eml(one)), _source(account, one))


def _clear(
    config: GraphConfig,
    account: str,
    *,
    page_size: int = PAGE_SIZE,
    delete_batch: int = DELETE_BATCH,
) -> PurgeCounts:
    with client.session(config) as graph:
        return purge_account(
            graph, account, page_size=page_size, delete_batch=delete_batch
        )


def _rows(config: GraphConfig, cypher: str) -> list:
    with client.session(config) as graph:
        return list(graph.execute(cypher).rows)


def _message_ids(config: GraphConfig) -> set[str]:
    return {row[0] for row in _rows(config, "MATCH (m:Message) RETURN m.id")}


def _held_by(config: GraphConfig, account: str) -> set[str]:
    """Which messages the graph still says came from this mailbox."""
    return {
        row[0]
        for row in _rows(
            config,
            "MATCH (m:Message)-[:ARCHIVED_FROM]->(a:Account) "
            f"WHERE a.id = '{account}' RETURN m.id",
        )
    }


def _counts(config: GraphConfig) -> tuple[int, int]:
    nodes = _rows(config, "MATCH (n) RETURN count(n)")[0][0]
    edges = _rows(config, "MATCH ()-[r]->() RETURN count(r)")[0][0]
    return int(nodes), int(edges)


class TestAMailboxOfItsOwn:
    def test_clearing_removes_every_message_it_imported(self, config) -> None:
        _archive(config, MINE, "m1@example.com", "m2@example.com")
        counts = _clear(config, MINE)

        assert counts.messages == 2
        assert counts.copies == 0
        assert _message_ids(config) == set()

    def test_the_message_edges_go_with_the_messages(self, config) -> None:
        """``DETACH DELETE`` and not ``DELETE``: an orphan edge is not legal."""
        _archive(config, MINE, "m1@example.com")

        _clear(config, MINE)

        assert _rows(config, "MATCH ()-[r]->() RETURN count(r)")[0][0] == 0

    def test_the_account_node_stays_because_the_mailbox_does(self, config) -> None:
        """Clearing is not deleting: the same mailbox is imported again next."""
        _archive(config, MINE, "m1@example.com")

        _clear(config, MINE)

        assert _rows(config, "MATCH (a:Account) RETURN a.id") == [[MINE]]

    def test_the_address_book_survives(self, config) -> None:
        """``Address`` carries ``remote_trusted`` — the one property a human wrote.

        Deleting the addresses would throw away a standing decision that has
        nothing to do with which mailbox the mail arrived in. They are left
        orphaned on purpose; the next import ``MERGE``\\ s them back into place.
        """
        _archive(config, MINE, "m1@example.com")

        _clear(config, MINE)

        addresses = {row[0] for row in _rows(config, "MATCH (a:Address) RETURN a.id")}
        assert addresses == {
            "anna@example.com",
            "bob@example.com",
            "dora@partner.example",
        }

    def test_a_second_clear_out_does_nothing_and_says_so(self, config) -> None:
        _archive(config, MINE, "m1@example.com")
        _clear(config, MINE)
        before = _counts(config)

        counts = _clear(config, MINE)

        assert (counts.messages, counts.copies) == (0, 0)
        assert _counts(config) == before

    def test_the_mailbox_can_be_imported_again_from_nothing(self, config) -> None:
        """The whole point of the feature, counted in the graph.

        A re-import after a clear-out has to land the same archive the first
        one did — not a partial one, and not a doubled one.
        """
        _archive(config, MINE, "m1@example.com", "m2@example.com")
        after_first_import = _counts(config)

        _clear(config, MINE)
        _archive(config, MINE, "m1@example.com", "m2@example.com")

        assert _counts(config) == after_first_import


class TestMailTwoMailboxesHold:
    """The contract the whole module is arranged around."""

    def test_a_shared_message_survives_with_the_other_mailboxs_copy(
        self, config
    ) -> None:
        _archive(config, MINE, "shared@example.com", "mine@example.com")
        _archive(config, THEIRS, "shared@example.com")

        counts = _clear(config, MINE)

        assert (counts.messages, counts.copies) == (1, 1)
        assert _message_ids(config) == {"shared@example.com"}
        assert _held_by(config, THEIRS) == {"shared@example.com"}

    def test_the_cleared_mailbox_no_longer_holds_anything(self, config) -> None:
        """Its provenance edge is dropped even where the message stays."""
        _archive(config, MINE, "shared@example.com")
        _archive(config, THEIRS, "shared@example.com")

        _clear(config, MINE)

        assert _held_by(config, MINE) == set()
        assert _held_by(config, THEIRS) == {"shared@example.com"}

    def test_the_surviving_copy_is_still_a_whole_message(self, config) -> None:
        """Not merely present — still readable, with its sender and its file.

        A clear-out that took the shared message's edges but left the node
        would pass every count above and hand the other mailbox a husk.
        """
        _archive(config, MINE, "shared@example.com")
        _archive(config, THEIRS, "shared@example.com")

        _clear(config, MINE)

        row = _rows(
            config,
            "MATCH (m:Message)-[:SENT_FROM]->(s:Address) "
            "MATCH (m)-[:HAS_ATTACHMENT]->(f:Attachment) "
            "RETURN m.subject, s.id, f.content_type",
        )[0]
        assert row[0] == "[PROJ-123] Angebot Q3"
        assert row[1] == "anna@example.com"
        assert row[2] == "application/pdf"

    def test_a_mailbox_that_shares_everything_loses_no_message(self, config) -> None:
        _archive(config, MINE, "one@example.com", "two@example.com")
        _archive(config, THEIRS, "one@example.com", "two@example.com")

        counts = _clear(config, MINE)

        assert (counts.messages, counts.copies) == (0, 2)
        assert _message_ids(config) == {"one@example.com", "two@example.com"}
        assert _held_by(config, THEIRS) == {"one@example.com", "two@example.com"}

    def test_clearing_the_other_mailbox_afterwards_finishes_the_job(
        self, config
    ) -> None:
        """The shared node belongs to nobody once both mailboxes are cleared."""
        _archive(config, MINE, "shared@example.com")
        _archive(config, THEIRS, "shared@example.com")
        _clear(config, MINE)

        counts = _clear(config, THEIRS)

        assert (counts.messages, counts.copies) == (1, 0)
        assert _message_ids(config) == set()


class TestPagingAndBatching:
    def test_a_page_smaller_than_the_archive_still_clears_all_of_it(
        self, config
    ) -> None:
        ids = [f"m{one}@example.com" for one in range(1, 8)]
        _archive(config, MINE, *ids)

        counts = _clear(config, MINE, page_size=2)

        assert counts.messages == 7
        assert _message_ids(config) == set()

    def test_a_batch_smaller_than_the_shared_set_still_drops_every_copy(
        self, config
    ) -> None:
        ids = [f"m{one}@example.com" for one in range(1, 6)]
        _archive(config, MINE, *ids)
        _archive(config, THEIRS, *ids)

        counts = _clear(config, MINE, page_size=2, delete_batch=2)

        assert (counts.messages, counts.copies) == (0, 5)
        assert _held_by(config, MINE) == set()
        assert _held_by(config, THEIRS) == set(ids)
