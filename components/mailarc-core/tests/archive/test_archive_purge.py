"""What the clear-out asks a graph for, and what it refuses to ask.

Two claims live here, and neither of them needs a graph.

The first is the **shape guard**. Deleting ground truth is the one thing in
this repository that cannot be undone by re-running anything, so the two
delete statements are matched character by character against a pattern at
import time. This file proves the guard bites — that a statement of the wrong
shape is rejected rather than run — the way
``mailarc-analytics``' ``test_derived_rebuild.py`` proves it for the derived
deletes it borrowed the device from.

The second is the **order of operations**. ``FakeSession`` answers the four
statements out of a dictionary and records every call, so the page loop, the
cursor, the exclusive/shared split and the deferred copy deletion are all
observable without a server. ``test_archive_purge_local.py`` proves the same
contract against a real FalkorDB, where the counts come from Cypher.
"""

import re
from typing import Any

import pytest
from runic.ogm import QueryBuilder, count, param, select

from mailarc_core.archive import purge
from mailarc_core.archive.model import Account, Message
from mailarc_core.archive.purge import PurgeCounts, purge_account

ACCOUNT = "7"
"""The mailbox being cleared — a SQLite row id as a string, as the writer spells it."""

OTHER = "9"
"""The mailbox that must come through a clear-out untouched."""


class FakeSession:
    """Answers the purge's four statements from a graph held in a dict.

    Not a stub with canned returns: the statements are told apart by their
    compiled Cypher, so a rewritten statement reaches the wrong branch and the
    test fails rather than passing on a shape nobody runs any more. What it
    models is the little the purge depends on — which messages an account
    holds, which of them somebody else holds too, and that a delete removes
    them.
    """

    def __init__(self, holdings: dict[str, set[str]]) -> None:
        self.holdings = {one: set(accounts) for one, accounts in holdings.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def all_rows(
        self, statement: QueryBuilder[Any], params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        cypher = purge._normalised(statement.build()[0])  # noqa: SLF001
        values = dict(params or {})
        if "DETACH DELETE m" in cypher:
            self.calls.append(("delete", values))
            return [{"removed": self._delete(values["ids"])}]
        if "DELETE r" in cypher:
            self.calls.append(("copies", values))
            return [{"removed": self._drop(values["account"], values["batch"])}]
        if "a.id <> $account" in cypher:
            self.calls.append(("shared", values))
            return self._shared(values["ids"], values["account"])
        self.calls.append(("page", values))
        return self._page(values["account"], values["after"], values["batch"])

    def _page(self, account: str, after: str, batch: int) -> list[dict[str, Any]]:
        held = sorted(
            one
            for one, accounts in self.holdings.items()
            if account in accounts and one > after
        )
        return [{"id": one} for one in held[:batch]]

    def _shared(self, ids: list[str], account: str) -> list[dict[str, Any]]:
        return [{"id": one} for one in ids if self.holdings.get(one, set()) - {account}]

    def _delete(self, ids: list[str]) -> int:
        removed = [one for one in ids if one in self.holdings]
        for one in removed:
            del self.holdings[one]
        return len(removed)

    def _drop(self, account: str, batch: int) -> int:
        dropped = 0
        for accounts in self.holdings.values():
            if dropped >= batch:
                break
            if account in accounts:
                accounts.discard(account)
                dropped += 1
        return dropped

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


def graph_of(holdings: dict[str, set[str]]) -> Any:
    """A :class:`FakeSession` handed over as a session.

    Untyped on the way out on purpose: ``purge_account`` wants a
    ``runic.ogm.Session`` and this is a stand-in for one, exactly as the
    writer's own tests hand their fake to ``MessageArchiver.archive``. Every
    test below then reads the fake's own attributes back off the same name.
    """
    return FakeSession(holdings)


class TestTheShapeGuard:
    """The delete statements are read character by character, not trusted."""

    def test_the_two_deletes_compile_to_exactly_the_pinned_shapes(self) -> None:
        """The guard passed at import; this is what it passed *on*."""
        assert purge._normalised(purge._delete_messages().build()[0]) == (  # noqa: SLF001
            "MATCH (m:Message) WHERE m.id IN $ids "
            "DETACH DELETE m RETURN count(m) AS removed"
        )
        assert purge._normalised(purge._delete_copies().build()[0]) == (  # noqa: SLF001
            "MATCH (a:Account) WHERE a.id = $account "
            "MATCH (a)<-[r:ARCHIVED_FROM]-(m:Message) "
            "WITH r LIMIT $batch DELETE r RETURN count(r) AS removed"
        )

    def test_the_copy_delete_narrows_before_it_deletes(self) -> None:
        """The whole reason that statement is rooted at the account.

        runic emits a predicate naming a traversed variable after the entire
        pipeline, so written from the message end the account filter lands
        *behind* the ``DELETE`` — which would drop every account's copy. The
        assertion is on the order of the two clauses and nothing else.
        """
        cypher = purge._normalised(purge._delete_copies().build()[0])  # noqa: SLF001

        assert cypher.index("WHERE a.id = $account") < cypher.index("DELETE r")

    def test_the_copy_delete_never_detaches(self) -> None:
        """``DETACH`` here would take the account and every other mailbox's mail."""
        assert "DETACH" not in purge._normalised(purge._delete_copies().build()[0])  # noqa: SLF001

    def test_an_unnarrowed_message_delete_is_refused(self) -> None:
        """The statement the guard exists to keep out of the archive."""
        unnarrowed = (
            select(purge.MESSAGE)
            .delete(detach=True)
            .returning(count("m").as_("removed"))
        )

        with pytest.raises(ValueError, match="unknown shape"):
            purge._verified(unnarrowed, purge._MESSAGE_DELETE)  # noqa: SLF001

    def test_a_detaching_copy_delete_is_refused(self) -> None:
        """Detaching an ``Account`` takes every mailbox's mail with it."""
        detaching = (
            select(purge.ACCOUNT)
            .where(purge.ACCOUNT.id == param("account"))
            .traverse(purge.ACCOUNT.copies, to=purge.MESSAGE, edge=purge.COPY)
            .with_(purge.COPY, limit=param("batch"))
            .delete(purge.COPY, detach=True)
            .returning(count("r").as_("removed"))
        )

        with pytest.raises(ValueError, match="unknown shape"):
            purge._verified(detaching, purge._COPY_DELETE)  # noqa: SLF001

    def test_reformatting_is_not_mistaken_for_tampering(self) -> None:
        """The shapes are matched against normalised Cypher, on purpose."""
        assert purge._normalised("MATCH  (m:`Message`)\n  WHERE\tx") == (  # noqa: SLF001
            "MATCH (m:Message) WHERE x"
        )


class TestClearingOneMailbox:
    def test_deletes_every_message_only_this_mailbox_holds(self) -> None:
        session = graph_of({"m1": {ACCOUNT}, "m2": {ACCOUNT}, "m3": {ACCOUNT}})

        counts = purge_account(session, ACCOUNT)

        assert counts == PurgeCounts(messages=3, copies=0)
        assert session.holdings == {}

    def test_keeps_a_message_another_mailbox_also_holds(self) -> None:
        """The contract the whole module is arranged around.

        ``m2`` reached two mailboxes and is one node with two edges. Clearing
        one of them may take the edge and may not take the node — the other
        mailbox's copy is somebody else's mail.
        """
        session = graph_of({"m1": {ACCOUNT}, "m2": {ACCOUNT, OTHER}})

        counts = purge_account(session, ACCOUNT)

        assert counts == PurgeCounts(messages=1, copies=1)
        assert session.holdings == {"m2": {OTHER}}

    def test_the_shared_copy_is_dropped_after_the_page_loop_not_during_it(self) -> None:
        """Order, and the reason for it: a message is never left unreachable.

        The copy deletion carries no id filter, so running it early would cut
        messages loose from the only account a later page could find them by.
        It is the last thing that happens, once every exclusive message is
        already gone.
        """
        session = graph_of({"m1": {ACCOUNT}, "m2": {ACCOUNT, OTHER}})

        purge_account(session, ACCOUNT)

        assert session.kinds()[-2:] == ["copies", "copies"]
        assert "copies" not in session.kinds()[:-2]

    def test_touches_nothing_when_no_copy_is_shared(self) -> None:
        """The unfiltered statement is not run at all in the ordinary case."""
        session = graph_of({"m1": {ACCOUNT}})

        purge_account(session, ACCOUNT)

        assert "copies" not in session.kinds()

    def test_leaves_another_mailbox_entirely_alone(self) -> None:
        session = graph_of({"m1": {ACCOUNT}, "m2": {OTHER}, "m3": {OTHER}})

        counts = purge_account(session, ACCOUNT)

        assert counts == PurgeCounts(messages=1, copies=0)
        assert session.holdings == {"m2": {OTHER}, "m3": {OTHER}}

    def test_an_already_cleared_mailbox_costs_one_read_and_no_write(self) -> None:
        """Re-runnable, and a second run is not a second delete."""
        session = graph_of({"m1": {OTHER}})

        counts = purge_account(session, ACCOUNT)

        assert counts == PurgeCounts()
        assert session.kinds() == ["page"]

    def test_pages_the_archive_and_carries_the_cursor_forward(self) -> None:
        """A cursor, never an offset — and never the same page twice."""
        session = graph_of({f"m{one}": {ACCOUNT} for one in range(1, 6)})

        counts = purge_account(session, ACCOUNT, page_size=2)

        pages = [values["after"] for kind, values in session.calls if kind == "page"]
        assert counts.messages == 5
        assert pages == ["", "m2", "m4", "m5"]

    def test_a_shared_copy_does_not_stall_the_page_loop(self) -> None:
        """The cursor is what makes this terminate at all.

        A shared message keeps its node *and* its edge until the very end, so
        a loop that read from the start each time would hand it back for ever.
        """
        session = graph_of({"m1": {ACCOUNT, OTHER}, "m2": {ACCOUNT}})

        counts = purge_account(session, ACCOUNT, page_size=1)

        assert counts == PurgeCounts(messages=1, copies=1)

    def test_reports_progress_as_the_count_grows(self) -> None:
        session = graph_of({f"m{one}": {ACCOUNT} for one in range(1, 5)})
        seen: list[int] = []

        purge_account(session, ACCOUNT, page_size=2, on_progress=seen.append)

        assert seen == [2, 4]

    def test_a_copy_it_never_classified_is_logged_and_not_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """What a concurrent import would look like from in here.

        The page loop saw one shared message; the graph gave up two edges. The
        edges are gone either way — but the mismatch means the archive held a
        copy this run never looked at, and that is a line in the log rather
        than a silent pass.
        """
        session = graph_of({"m1": {ACCOUNT, OTHER}, "m2": {ACCOUNT, OTHER}})
        # Only the first of the two shared copies is ever paged, so the loop
        # classifies one and the graph gives up both.
        session._page = lambda account, after, batch: (  # noqa: SLF001
            [{"id": "m1"}] if after == "" else []
        )

        with caplog.at_level("WARNING"):
            counts = purge_account(session, ACCOUNT)

        assert counts.copies == 2
        assert "where 1 were expected" in caplog.text


class TestTheStatementsAreNotShared:
    def test_every_call_builds_its_own(self) -> None:
        """A statement is bound to a session while it runs, so it cannot be a
        module-level constant two threads reach at once — which is the trap
        ``mailarc_analytics.queries.rows`` had to put a lock around.
        """
        assert purge._page_ids() is not purge._page_ids()  # noqa: SLF001
        assert purge._delete_messages() is not purge._delete_messages()  # noqa: SLF001


def test_the_page_statement_skips_nodes_without_a_canonical_id() -> None:
    """An older graph can hold one, and the listing skips it for the same reason."""
    cypher = purge._normalised(purge._page_ids().build()[0])  # noqa: SLF001

    assert "m.id IS NOT NULL" in cypher
    assert "m.id > $after" in cypher
    assert re.search(r"ORDER BY id ASC LIMIT \$batch", cypher)


def test_the_page_statement_is_scoped_to_one_account() -> None:
    """Without this predicate the clear-out reads the whole archive."""
    assert "WHERE a.id = $account" in purge._normalised(purge._page_ids().build()[0])  # noqa: SLF001


def test_the_shared_statement_asks_for_other_accounts_only() -> None:
    """``<>`` and not ``=``: it looks for the mailboxes that are not this one."""
    cypher = purge._normalised(purge._shared_ids().build()[0])  # noqa: SLF001

    assert "WHERE a.id <> $account" in cypher
    assert "m.id IN $ids" in cypher


def test_the_account_relation_walks_the_edge_the_writer_writes() -> None:
    """``Account.copies`` is a second view of ``Message.archived_from``.

    Declared so a statement can be rooted at the account; it must name the
    same relationship type and point back at the node the writer writes from,
    or the clear-out would delete along an edge nothing creates.

    Read through :func:`getattr` because a ``Relation`` descriptor is annotated
    as the type it *resolves* to on an instance, and the class-level object is
    the descriptor itself.
    """
    from_account: Any = getattr(Account, "copies")  # noqa: B009 - see docstring
    from_message: Any = getattr(Message, "archived_from")  # noqa: B009

    assert from_account.relationship == from_message.relationship == "ARCHIVED_FROM"
    assert from_account.direction == "INCOMING"
    assert from_message.direction == "OUTGOING"
    assert from_account.target == "Message"
