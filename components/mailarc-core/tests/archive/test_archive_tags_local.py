"""The annotation layer against a real FalkorDB.

``test_archive_tags.py`` proves the shapes and the membership filter without a
server. What needs one is everything the shapes were chosen *for*:

* that ``untag`` removes an edge and leaves both nodes standing — a
  ``DETACH`` there passes every unit test in the file next door and deletes
  mail;
* that a second tagging really keeps the first ``at`` and the first ``source``,
  which is a claim about what is stored, not about which rows were sent;
* that clearing a mailbox leaves the tag behind with a count of zero, which is
  the whole reason the annotation layer lives in ``mailarc-core`` and not
  beside the derived nodes;
* that the ``UNIQUE`` constraint the migration creates is what actually stops a
  duplicate — the repository's lookup cannot, and does not claim to.

The server fixtures live in ``conftest.py`` and are shared with the writer's,
the reader's and the purge's local tests: SESSION scoped and torn down
explicitly, with each test isolating itself through a graph name of its own.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import partial
from typing import Any

import pytest
from runic.migrate.adapters import create_adapter
from runic.migrate.operations import GraphOperations

from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource, Tag, TagOrigin, TagSource
from mailarc_core.archive.purge import purge_account
from mailarc_core.archive.tags import TagExists, TagRepository, TagStore, tag_id
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.model import MailProvider
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local

MINE = "7"
"""The mailbox the messages are archived from — the ``Account`` node's key."""


def _eml(message_id: str, subject: str = "Angebot Q3") -> bytes:
    """One message, distinguished by the id its canonical id derives from."""
    return f"""\
From: Anna Bauer <anna@example.com>
To: Bob Baker <bob@example.com>
Subject: [NORD-42] {subject}
Date: Wed, 04 Mar 2026 09:15:00 +0100
Message-ID: <{message_id}>
Content-Type: text/plain; charset="utf-8"

Hallo Bob,

anbei das Angebot.

Anna
""".encode()


@pytest.fixture
def archived(config: GraphConfig) -> tuple[str, ...]:
    """Three messages in the graph, and their canonical ids in order."""
    archiver = MessageArchiver(ArchiveConfig())
    written: list[str] = []
    with client.session(config) as graph:
        for one in ("a@example.com", "b@example.com", "c@example.com"):
            result = archiver.archive(
                graph,
                parse_message(_eml(one)),
                ArchiveSource(
                    account_id=MINE,
                    account_address="anna@example.com",
                    provider=MailProvider.GMAIL,
                    provider_message_id=f"g-{one}",
                ),
            )
            written.append(result.canonical_id)
    return tuple(written)


@pytest.fixture
def store(config: GraphConfig) -> TagStore:
    """The façade a page would hold — a session per call, on this test's graph."""
    return TagStore(partial(client.session, config))


@pytest.fixture
def constrained(config: GraphConfig) -> Iterator[GraphConfig]:
    """This test's graph with the migration's ``UNIQUE`` constraint applied.

    Through runic's real operations object rather than a hand-written
    ``GRAPH.CONSTRAINT`` command: what is in doubt is whether the constraint the
    migration asks for is the one that rejects the second write, and a
    transcription would only prove the transcription.
    """
    adapter = create_adapter(
        "falkordb", host=config.host, port=config.port, graph_name=config.graph_name
    )
    try:
        GraphOperations(adapter).create_constraint("UNIQUE", "NODE", "Tag", ["id"])
        yield config
    finally:
        close = getattr(adapter, "close", None)
        if close is not None:
            close()


def _rows(config: GraphConfig, cypher: str) -> list[Any]:
    with client.session(config) as graph:
        return list(graph.execute(cypher).rows)


def _count(config: GraphConfig, cypher: str) -> int:
    return int(_rows(config, cypher)[0][0])


class TestTheTagItself:
    def test_it_is_created_renamed_recoloured_and_deleted(
        self, config: GraphConfig, store: TagStore
    ) -> None:
        summary = store.create("NORD-42", origin=TagOrigin.MANUAL, color="#c94f4f")

        assert summary.id == "tag:nord-42"
        assert store.rename(summary.id, "Nordlicht 42") is True
        assert store.recolor(summary.id, "#3a7d5f") is True
        assert store.list_tags()[0].color == "#3a7d5f"

        # Clearing is the half dirty tracking cannot express — runic's update
        # encodes only properties that have a value.
        assert store.recolor(summary.id, None) is True

        listed = store.list_tags()
        assert [(one.id, one.name, one.color) for one in listed] == [
            ("tag:nord-42", "Nordlicht 42", None)
        ]
        assert listed[0].origin is TagOrigin.MANUAL
        assert listed[0].created_at is not None

        assert store.delete(summary.id) is True
        assert store.list_tags() == ()
        assert _count(config, "MATCH (t:Tag) RETURN count(t)") == 0

    def test_a_rename_does_not_move_the_key(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        """The id is what every membership points at. Re-keying on a rename
        would orphan the whole tag — which is the one thing here that no re-run
        repairs."""
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, list(archived))

        store.rename(summary.id, "Something else entirely")

        assert store.members(summary.id) != ()
        assert store.list_tags()[0].id == "tag:nord-42"

    def test_deleting_one_that_is_not_there_says_so(self, store: TagStore) -> None:
        assert store.delete("tag:never-existed") is False

    def test_renaming_one_that_is_not_there_says_so(self, store: TagStore) -> None:
        assert store.rename("tag:never-existed", "x") is False

    def test_the_repository_refuses_a_duplicate_name(self, config: GraphConfig) -> None:
        with client.session(config) as graph:
            repository = TagRepository(graph)
            repository.create("NORD-42")

            with pytest.raises(TagExists):
                repository.create("nord 42")

    def test_the_unique_constraint_refuses_a_second_tag_with_the_same_id(
        self, constrained: GraphConfig
    ) -> None:
        """The backstop behind the lookup, and the reason the lookup is not the
        guarantee: two sessions can both find nothing and both write. Reached
        past the repository on purpose — its own check would answer first, and
        what is under test here is the graph's.

        Matched on the message and not on the type. FalkorDB answers a
        violation with ``redis.exceptions.ResponseError: unique constraint
        violation on node of type Tag``, and importing the driver's exception
        into a component test would name the backend this package spent
        ``GraphConfig.backend`` avoiding.

        Without the constraint this test does not raise at all — checked by
        emptying the fixture and watching it fail — so the fixture is doing the
        work the assertion claims.
        """
        key = tag_id("NORD-42")
        with client.session(constrained) as graph:
            graph.add(Tag(id=key, name="NORD-42", origin=TagOrigin.MANUAL))
            graph.flush()

        with (
            pytest.raises(Exception, match="unique constraint violation"),
            client.session(constrained) as graph,
        ):
            graph.add(Tag(id=key, name="NORD-42 again"))
            graph.flush()

        assert _count(constrained, "MATCH (t:Tag) RETURN count(t)") == 1


class TestMembership:
    def test_tagging_twice_keeps_the_first_decision(
        self, config: GraphConfig, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        """The invariant the read in front of the write exists for. A message
        tagged by hand in March must not come back as ``auto``, dated today,
        the first time an accepted suggestion names it again."""
        summary = store.create("NORD-42")
        first = datetime(2026, 3, 4, 9, 15, tzinfo=UTC)
        store.tag_messages(summary.id, [archived[0]], source=TagSource.MANUAL, at=first)

        again = store.tag_messages(
            summary.id,
            [archived[0]],
            source=TagSource.AUTO,
            at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        )

        assert again == 0
        stored = _rows(
            config, "MATCH (:Message)-[r:TAGGED]->(:Tag) RETURN r.source, r.at"
        )
        assert stored == [["manual", first.isoformat()]]

    def test_a_second_message_is_still_added(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        """Keeping the first decision must not mean refusing the batch."""
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, [archived[0]])

        written = store.tag_messages(summary.id, [archived[0], archived[1]])

        assert written == 1
        assert set(store.members(summary.id)) == {archived[0], archived[1]}

    def test_a_message_the_archive_does_not_hold_writes_nothing(
        self, store: TagStore
    ) -> None:
        summary = store.create("NORD-42")

        assert store.tag_messages(summary.id, ["not-a-message"]) == 0
        assert store.members(summary.id) == ()

    def test_untag_leaves_the_message_and_the_tag(
        self, config: GraphConfig, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        """``DELETE r`` and never ``DETACH``: the shape guard's claim, made
        against a graph rather than against a regex."""
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, list(archived))
        before = _count(config, "MATCH (m:Message) RETURN count(m)")

        removed = store.untag(summary.id, [archived[0]])

        assert removed == 1
        assert _count(config, "MATCH (m:Message) RETURN count(m)") == before
        assert _count(config, "MATCH (t:Tag) RETURN count(t)") == 1
        assert set(store.members(summary.id)) == {archived[1], archived[2]}

    def test_untag_takes_only_the_messages_it_was_given(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        """Without the ``WITH r, m`` stage runic puts the id predicate behind
        the ``DELETE`` and this empties the tag while reporting the right
        number."""
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, list(archived))

        store.untag(summary.id, [archived[0]])

        assert len(store.members(summary.id)) == 2

    def test_untagging_a_message_that_never_wore_it_removes_nothing(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, [archived[0]])

        assert store.untag(summary.id, [archived[1]]) == 0
        assert store.members(summary.id) == (archived[0],)


class TestTheReads:
    def test_the_listing_counts_the_members(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        empty = store.create("Leer")
        full = store.create("NORD-42")
        store.tag_messages(full.id, list(archived))

        counted = {one.id: one.message_count for one in store.list_tags()}

        assert counted == {empty.id: 0, full.id: 3}

    def test_tags_of_answers_per_message_and_omits_the_untagged(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        first = store.create("NORD-42")
        second = store.create("Angebote")
        store.tag_messages(first.id, [archived[0], archived[1]])
        store.tag_messages(second.id, [archived[0]])

        found = store.tags_of(list(archived))

        assert set(found) == {archived[0], archived[1]}
        assert [one.id for one in found[archived[0]]] == [second.id, first.id]
        assert [one.id for one in found[archived[1]]] == [first.id]

    def test_a_summary_from_a_read_carries_no_count(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        """Only the listing counts. Every other read leaves it at zero rather
        than paying for a traversal nobody asked for."""
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, list(archived))

        assert store.tags_of([archived[0]])[archived[0]][0].message_count == 0

    def test_members_pages(self, store: TagStore, archived: tuple[str, ...]) -> None:
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, list(archived))

        first = store.members(summary.id, limit=2)
        second = store.members(summary.id, limit=2, offset=2)

        assert len(first) == 2
        assert len(second) == 1
        assert set(first) | set(second) == set(archived)


class TestPromotion:
    def test_it_creates_the_tag_and_its_members_in_one_gesture(
        self, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        summary = store.promote("NORD-42", archived, origin=TagOrigin.TOPIC)

        assert summary.id == "tag:nord-42"
        assert summary.origin is TagOrigin.TOPIC
        assert summary.message_count == 3
        assert set(store.members(summary.id)) == set(archived)

    def test_the_membership_records_that_it_was_accepted(
        self, config: GraphConfig, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        store.promote("NORD-42", [archived[0]], origin=TagOrigin.COMMUNITY)

        assert _rows(
            config, "MATCH (:Message)-[r:TAGGED]->(:Tag) RETURN DISTINCT r.source"
        ) == [["accepted"]]


class TestClearingAMailbox:
    def test_the_tag_survives_with_a_count_of_zero(
        self, config: GraphConfig, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        """The whole reason the annotation layer sits in ``mailarc-core``. The
        messages this mailbox was the sole holder of are gone, their ``TAGGED``
        edges with them — and the tag stands, empty and deletable, rather than
        vanishing with the mail it named."""
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, list(archived))

        with client.session(config) as graph:
            purge_account(graph, MINE)

        assert _count(config, "MATCH (m:Message) RETURN count(m)") == 0
        assert [(one.id, one.message_count) for one in store.list_tags()] == [
            (summary.id, 0)
        ]

    def test_the_empty_tag_can_still_be_deleted(
        self, config: GraphConfig, store: TagStore, archived: tuple[str, ...]
    ) -> None:
        summary = store.create("NORD-42")
        store.tag_messages(summary.id, list(archived))
        with client.session(config) as graph:
            purge_account(graph, MINE)

        assert store.delete(summary.id) is True
        assert store.list_tags() == ()
