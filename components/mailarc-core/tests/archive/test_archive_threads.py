"""What the conversation repair asks a graph for, and what it does with it.

No server. ``FakeSession`` answers the module's three statements out of a
dictionary and records every write, so the page loop, the cursor, the key rule
and the two counts are observable here; ``test_archive_threads_local.py``
proves the same contract against a real FalkorDB.

The claim under test is the one the archive got wrong for a year: a message
that carries neither ``References`` nor ``In-Reply-To`` is the *first* message
of its conversation, and the thread its own replies will be filed under is
keyed on its ``Message-ID``. Every message imported before the writer learned
that rule sits outside every conversation, and no re-sync reaches it — the
import engine drops what the ledger already knows.
"""

from typing import Any

from runic.ogm import QueryBuilder

from mailarc_core.archive import threads
from mailarc_core.archive.model import Message, Thread
from mailarc_core.archive.threads import (
    ThreadRepairCounts,
    count_unthreaded,
    repair_threads,
)

ACCOUNT = "5"
"""The mailbox every message below was archived from."""


class Row:
    """One message as the fake graph holds it: provenance, and a thread or not."""

    def __init__(
        self,
        message_id: str,
        *,
        rfc_message_id: str | None = None,
        subject: str = "Einladung",
        thread: str | None = None,
        accounts: tuple[str, ...] = (ACCOUNT,),
        provider_thread_id: str | None = None,
    ) -> None:
        self.message_id = message_id
        self.rfc_message_id = message_id if rfc_message_id is None else rfc_message_id
        self.subject = subject
        self.thread = thread
        self.accounts = accounts
        self.provider_thread_id = provider_thread_id


class FakeSession:
    """Answers the repair's statements from a graph held in a dict.

    Told apart by their compiled Cypher, like the purge's fake: a rewritten
    statement reaches the wrong branch and the test fails rather than passing
    over a shape nobody runs any more.
    """

    def __init__(self, rows: list[Row]) -> None:
        self.rows = {one.message_id: one for one in rows}
        self.threads: dict[str, Thread] = {}
        self.added: list[Thread] = []
        self.related: list[tuple[str, str]] = []
        self.flushes = 0

    def all_rows(
        self, statement: QueryBuilder[Any], params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        cypher = threads._normalised(statement.build()[0])  # noqa: SLF001
        values = dict(params or {})
        if "IN_THREAD" in cypher and "count(" in cypher:
            return [{"total": len({one for one in self.rows.values() if one.thread})}]
        if "count(" in cypher:
            return [{"total": len(self.rows)}]
        if "IN_THREAD" in cypher:
            return [
                {"message_id": one.message_id}
                for one in self._asked(values["ids"])
                if one.thread
            ]
        if "ARCHIVED_FROM" in cypher:
            return [
                {
                    "message_id": one.message_id,
                    "rfc_message_id": one.rfc_message_id,
                    "subject": one.subject,
                    "account_id": account,
                    "provider_thread_id": one.provider_thread_id,
                }
                for one in self._asked(values["ids"])
                for account in one.accounts
            ]
        if "Thread" in cypher:
            return [{"id": one} for one in values["keys"] if one in self.threads]
        return self._page(values)

    def add(self, node: Thread) -> None:
        self.added.append(node)
        self.threads[node.id] = node

    def flush(self) -> None:
        self.flushes += 1

    def relate(self, source: Message, field: Any, target: Thread) -> None:
        self.related.append((source.id, target.id))
        self.rows[source.id].thread = target.id

    def _page(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        after = str(values["after"])
        listed = sorted(one for one in self.rows if one > after)
        return [{"id": one} for one in listed[: int(values["batch"])]]

    def _asked(self, ids: list[str]) -> list[Row]:
        return [self.rows[one] for one in ids if one in self.rows]


def _session(*rows: Row) -> Any:
    """A :class:`FakeSession` handed over as a session.

    Untyped on the way out on purpose, the way the purge's ``graph_of`` is:
    the repair wants a ``runic.ogm.Session`` and this is a stand-in for one.
    Every test below then reads the fake's own attributes back off the same
    name.
    """
    return FakeSession(list(rows))


class TestWhatItLinks:
    def test_a_root_joins_the_thread_its_replies_already_made(self) -> None:
        """The archive's actual defect, in three messages.

        The reply carries ``References: <root>`` and was filed under
        ``5:root@example.com`` at import; the root carries no reference at all
        and was filed under nothing. The thread it belongs in is the one keyed
        on its own ``Message-ID``, which already exists.
        """
        session = _session(
            Row("reply@example.com", thread=f"{ACCOUNT}:root@example.com"),
            Row("root@example.com"),
        )
        session.threads[f"{ACCOUNT}:root@example.com"] = Thread(
            id=f"{ACCOUNT}:root@example.com"
        )

        counts = repair_threads(session)

        assert session.related == [("root@example.com", f"{ACCOUNT}:root@example.com")]
        assert counts == ThreadRepairCounts(scanned=2, linked=1, threads=0, unkeyed=0)

    def test_a_message_no_reply_ever_answered_opens_its_own_thread(self) -> None:
        """One ``Thread`` node per standalone message — the writer's own cost.

        Paid for the same reason it is paid on import: the node is what a
        reply arriving tomorrow is merged onto, and a conversation of one
        draws as a plain row, so nothing on screen changes today.
        """
        counts = repair_threads(_session(Row("alone@example.com")))

        assert counts == ThreadRepairCounts(scanned=1, linked=1, threads=1, unkeyed=0)

    def test_the_provider_thread_id_still_wins(self) -> None:
        """The writer's precedence, unchanged: a provider that groups is right.

        Gmail and Microsoft 365 hand out a conversation id of their own and it
        is on the provenance edge. Keying such a message on its ``Message-ID``
        instead would file it away from the siblings the provider already
        grouped it with.
        """
        session = _session(
            Row("m@example.com", provider_thread_id="t-42"),
        )

        repair_threads(session)

        assert session.related == [("m@example.com", f"{ACCOUNT}:t-42")]

    def test_the_same_mail_in_two_mailboxes_joins_both_conversations(self) -> None:
        """One node, one edge per account — a ``Thread`` is keyed per mailbox.

        :class:`~mailarc_core.archive.model.Thread` is scoped to the account
        on purpose, so a message archived twice sits in each mailbox's own
        conversation, which is exactly what the writer does on import.
        """
        session = _session(Row("m@example.com", accounts=("5", "7")))

        counts = repair_threads(session)

        assert sorted(session.related) == [
            ("m@example.com", "5:m@example.com"),
            ("m@example.com", "7:m@example.com"),
        ]
        assert counts.linked == 2
        assert counts.threads == 2

    def test_a_message_already_in_a_conversation_is_left_alone(self) -> None:
        session = _session(Row("m@example.com", thread="5:t-1"))

        counts = repair_threads(session)

        assert session.related == []
        assert counts == ThreadRepairCounts(scanned=1, linked=0, threads=0, unkeyed=0)

    def test_running_it_twice_writes_nothing_the_second_time(self) -> None:
        """Re-runnable, because the fix is a button somebody can press twice."""
        session = _session(Row("root@example.com"), Row("alone@example.com"))
        repair_threads(session)
        session.related.clear()

        counts = repair_threads(session)

        assert session.related == []
        assert counts == ThreadRepairCounts(scanned=2, linked=0, threads=0, unkeyed=0)


class TestWhatItRefuses:
    def test_a_message_without_a_message_id_is_counted_and_skipped(self) -> None:
        """A ``sha256:`` canonical id is a digest of bytes, not an address.

        No reply can ever reference one, so a thread keyed on it would be a
        node nothing could join — the writer refuses the same case, and this
        counts it rather than passing over it in silence.
        """
        session = _session(Row("sha256:abc", rfc_message_id=""))

        counts = repair_threads(session)

        assert session.related == []
        assert counts == ThreadRepairCounts(scanned=1, linked=0, threads=0, unkeyed=1)

    def test_a_message_no_account_holds_is_counted_and_skipped(self) -> None:
        """A ``Thread`` id needs a mailbox to be scoped to; nothing else will do."""
        session = _session(Row("m@example.com", accounts=()))

        counts = repair_threads(session)

        assert session.related == []
        assert counts.unkeyed == 1


class TestHowItWalks:
    def test_it_pages_by_cursor_until_the_graph_stops_answering(self) -> None:
        session = _session(*[Row(f"m{one}@example.com") for one in range(5)])

        counts = repair_threads(session, page_size=2)

        assert counts.scanned == 5
        assert len(session.related) == 5

    def test_it_reports_progress_after_every_page(self) -> None:
        session = _session(*[Row(f"m{one}@example.com") for one in range(5)])
        seen: list[int] = []

        repair_threads(session, page_size=2, on_progress=seen.append)

        assert seen == [2, 4, 5]

    def test_an_empty_archive_costs_one_read_and_writes_nothing(self) -> None:
        session = _session()

        counts = repair_threads(session)

        assert counts == ThreadRepairCounts()
        assert session.related == []

    def test_the_threads_reach_the_graph_before_the_edges(self) -> None:
        """A ``MERGE`` over two ``MATCH`` clauses writes nothing when one misses.

        The writer states it — "relate a node that has not reached the graph
        yet and the edge is silently not written" — and a repair that flushed
        afterwards would report edges it never wrote.
        """
        session = _session(Row("alone@example.com"))

        repair_threads(session)

        assert session.flushes >= 1


class TestTheCount:
    def test_it_is_the_messages_no_conversation_holds(self) -> None:
        session = _session(
            Row("a@example.com", thread="5:t-1"),
            Row("b@example.com"),
            Row("c@example.com"),
        )

        assert count_unthreaded(session) == 2

    def test_an_archive_that_is_whole_says_nothing_is_missing(self) -> None:
        session = _session(Row("a@example.com", thread="5:t-1"))

        assert count_unthreaded(session) == 0

    def test_it_never_reports_a_negative(self) -> None:
        """Two counts, read one after the other, over a graph an import writes.

        A message archived between the two reads makes the second larger than
        the first, and a panel must not offer to repair minus one message.
        """

        class Racing(FakeSession):
            def all_rows(
                self,
                statement: QueryBuilder[Any],
                params: dict[str, Any] | None = None,
            ) -> list[dict[str, Any]]:
                cypher = threads._normalised(statement.build()[0])  # noqa: SLF001
                if "count(" in cypher:
                    return [{"total": 3 if "IN_THREAD" in cypher else 1}]
                return super().all_rows(statement, params)

        racing: Any = Racing([])

        assert count_unthreaded(racing) == 0
