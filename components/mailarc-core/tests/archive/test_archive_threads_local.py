"""Repairing conversations, proved against a real FalkorDB.

Skipped unless `task tauri:vendor` has produced the runtime, so a checkout
without it still runs green. ``test_archive_threads.py`` proves the same claims
against a fake session; this file is what makes them claims about a *graph* —
every count below comes from Cypher.

**How the defect is reproduced.** The writer of today gives every message a
conversation, so an archive it wrote has nothing to repair. What broke real
archives is the writer of a year ago, which keyed a thread on the provider's id
or on ``References`` and on nothing else: a conversation's first message got no
node at all. Each test therefore imports normally and then drops the edges that
writer would never have written — which is exactly the graph a person who
imported their mail before the fallback landed is looking at now.

The claim worth the server is the one a fake cannot make: relating by handle
writes an ``IN_THREAD`` edge and **nothing else** — no second ``Message`` node
for the id it matched on, no property rewritten on either end.
"""

import pytest

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource
from mailarc_core.archive.repository import ThreadRepository
from mailarc_core.archive.threads import (
    ThreadRepairCounts,
    count_unthreaded,
    repair_threads,
)
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.model import MailProvider
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local

MINE = "5"
"""The mailbox under test — the ``Account`` node key, as the writer spells it."""

ROOT = "root@example.com"
"""The message that opens the conversation, and carries no reference at all."""

REPLY = "reply@example.com"
"""Its answer, which names the root in ``References`` and was always threaded."""


def _eml(message_id: str, *, subject: str, references: str = "") -> bytes:
    """One message; a reference header only where the exchange has one."""
    header = (
        f"References: <{references}>\nIn-Reply-To: <{references}>\n"
        if references
        else ""
    )
    return f"""\
From: Anna Bauer <anna@example.com>
To: Bob Baker <bob@example.com>
Subject: {subject}
Date: Wed, 04 Mar 2026 09:15:00 +0100
Message-ID: <{message_id}>
{header}Content-Type: text/plain; charset="utf-8"

Hallo Bob,

{subject}

Anna
""".encode()


def _source(account: str, message_id: str, *, thread_id: str = "") -> ArchiveSource:
    return ArchiveSource(
        account_id=account,
        account_address=f"anna{account}@example.com",
        provider=MailProvider.IMAP,
        provider_message_id=f"i-{account}-{message_id}",
        provider_thread_id=thread_id or None,
        folder="INBOX",
    )


def _archive(
    config: GraphConfig, *messages: tuple[str, bytes], account: str = MINE
) -> None:
    archiver = MessageArchiver(ArchiveConfig())
    with client.session(config) as graph:
        for message_id, raw in messages:
            archiver.archive(graph, parse_message(raw), _source(account, message_id))


def _rows(config: GraphConfig, cypher: str) -> list:
    with client.session(config) as graph:
        return list(graph.execute(cypher).rows)


def _unthread(config: GraphConfig, *message_ids: str) -> None:
    """Drop these messages' ``IN_THREAD`` edges — the old writer's archive."""
    listed = ", ".join(f"'{one}'" for one in message_ids)
    _rows(
        config,
        f"MATCH (m:Message)-[r:IN_THREAD]->(:Thread) WHERE m.id IN [{listed}] DELETE r",
    )


def _drop_threads(config: GraphConfig) -> None:
    """Take the ``Thread`` nodes out too, edges and all."""
    _rows(config, "MATCH (t:Thread) DETACH DELETE t")


def _repair(config: GraphConfig, *, page_size: int = 500) -> ThreadRepairCounts:
    with client.session(config) as graph:
        return repair_threads(graph, page_size=page_size)


def _unthreaded(config: GraphConfig) -> int:
    with client.session(config) as graph:
        return count_unthreaded(graph)


def _conversations(config: GraphConfig, *ids: str) -> dict[str, str]:
    with client.session(config) as graph:
        return ThreadRepository(graph).find_for_messages(list(ids))


def _counts(config: GraphConfig) -> tuple[int, int, int]:
    messages = _rows(config, "MATCH (m:Message) RETURN count(m)")[0][0]
    threads = _rows(config, "MATCH (t:Thread) RETURN count(t)")[0][0]
    edges = _rows(config, "MATCH ()-[r:IN_THREAD]->() RETURN count(r)")[0][0]
    return int(messages), int(threads), int(edges)


def _exchange(config: GraphConfig) -> None:
    """The archive's actual defect: a threaded reply and its unthreaded root."""
    _archive(
        config,
        (ROOT, _eml(ROOT, subject="Einladung zur Eigentümerversammlung")),
        (REPLY, _eml(REPLY, subject="AW: Einladung", references=ROOT)),
    )
    _unthread(config, ROOT)


class TestTheRootRejoinsItsReplies:
    def test_the_two_end_up_in_one_conversation(self, config) -> None:
        _exchange(config)
        assert _conversations(config, ROOT, REPLY) == {REPLY: f"{MINE}:{ROOT}"}

        _repair(config)

        assert _conversations(config, ROOT, REPLY) == {
            ROOT: f"{MINE}:{ROOT}",
            REPLY: f"{MINE}:{ROOT}",
        }

    def test_it_joins_the_thread_that_is_there_rather_than_making_one(
        self, config
    ) -> None:
        _exchange(config)

        counts = _repair(config)

        assert (counts.linked, counts.threads, counts.unkeyed) == (1, 0, 0)
        assert _counts(config)[1] == 1

    def test_the_conversation_then_counts_both_of_its_messages(self, config) -> None:
        """What the heading prints. ``1 of 2`` was the symptom on the screen."""
        _exchange(config)

        _repair(config)

        with client.session(config) as graph:
            totals = ThreadRepository(graph).count_members([f"{MINE}:{ROOT}"])
        assert totals == {f"{MINE}:{ROOT}": 2}

    def test_nothing_but_the_edge_is_written(self, config) -> None:
        """Relating by handle must not leave a second ``Message`` node behind.

        ``Session.relate`` matches both ends on their primary key and merges
        the relationship, so a handle carrying only an id is enough — and if
        that ever stopped being true, an id-less shell of every repaired
        message would appear in the graph. Counted rather than trusted.
        """
        _exchange(config)
        before = _rows(config, "MATCH (n) RETURN count(n)")[0][0]

        _repair(config)

        assert _rows(config, "MATCH (n) RETURN count(n)")[0][0] == before
        assert _rows(config, "MATCH (m:Message) RETURN m.subject ORDER BY m.id") == [
            ["AW: Einladung"],
            ["Einladung zur Eigentümerversammlung"],
        ]


class TestAnArchiveWithNoConversationsAtAll:
    def test_every_message_gets_the_thread_its_own_id_names(self, config) -> None:
        """The pre-fallback archive of somebody who never replied to anything."""
        _archive(
            config,
            (ROOT, _eml(ROOT, subject="Einladung")),
            ("other@example.com", _eml("other@example.com", subject="Rechnung")),
        )
        _drop_threads(config)

        counts = _repair(config)

        assert (counts.scanned, counts.linked, counts.threads) == (2, 2, 2)
        assert _counts(config)[1:] == (2, 2)

    def test_a_second_run_writes_nothing(self, config) -> None:
        _exchange(config)
        _repair(config)
        before = _counts(config)

        counts = _repair(config)

        assert counts == ThreadRepairCounts(scanned=2)
        assert _counts(config) == before

    def test_it_pages_through_an_archive_larger_than_one_page(self, config) -> None:
        _archive(
            config,
            *[
                (
                    f"m{one}@example.com",
                    _eml(f"m{one}@example.com", subject=f"Nr {one}"),
                )
                for one in range(5)
            ],
        )
        _drop_threads(config)

        counts = _repair(config, page_size=2)

        assert (counts.scanned, counts.linked) == (5, 5)
        assert _unthreaded(config) == 0


class TestTheCount:
    def test_it_names_the_messages_no_conversation_holds(self, config) -> None:
        _exchange(config)

        assert _unthreaded(config) == 1

    def test_it_is_zero_once_the_repair_has_run(self, config) -> None:
        _exchange(config)

        _repair(config)

        assert _unthreaded(config) == 0

    def test_an_empty_archive_has_nothing_to_repair(self, config) -> None:
        assert _unthreaded(config) == 0
        assert _repair(config) == ThreadRepairCounts()


class TestWhatItLeavesAlone:
    def test_a_message_the_provider_grouped_keeps_the_provider_key(
        self, config
    ) -> None:
        """A mailbox that hands out conversation ids is repaired by its own.

        Gmail and Microsoft 365 put one on every message, and it is on the
        provenance edge; keying such a message on its ``Message-ID`` would
        file it away from the siblings the provider grouped it with.
        """
        archiver = MessageArchiver(ArchiveConfig())
        with client.session(config) as graph:
            archiver.archive(
                graph,
                parse_message(_eml(ROOT, subject="Einladung")),
                _source(MINE, ROOT, thread_id="t-42"),
            )
        _unthread(config, ROOT)

        _repair(config)

        assert _conversations(config, ROOT) == {ROOT: f"{MINE}:t-42"}

    def test_mail_that_is_already_whole_is_left_exactly_as_it_was(self, config) -> None:
        _archive(
            config,
            (ROOT, _eml(ROOT, subject="Einladung")),
            (REPLY, _eml(REPLY, subject="AW: Einladung", references=ROOT)),
        )
        before = _counts(config)

        counts = _repair(config)

        assert counts == ThreadRepairCounts(scanned=2)
        assert _counts(config) == before
