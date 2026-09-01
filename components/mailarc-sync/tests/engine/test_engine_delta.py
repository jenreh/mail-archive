"""The incremental half of the pipeline: watermarks, deltas and stale cursors.

Split out of ``test_engine_run.py`` when phase 7 pushed that file past the
thousand lines AGENTS.md §5 allows. The seam is the mode: everything here
drives :meth:`ImportEngine.run` with a cursor kind in play — what a full walk
leaves behind for the first delta, what a delta fetches, what the very first
one does when there is no history to ask about, and what happens when the
provider says the cursor is too old.

Same world as the full-run module (``conftest.py``) and the same stand-ins
(``engine_doubles.py``), so a claim made here is made against the real engine,
the real parser, a real blob store and a real SQLite file.
"""

import pytest

from engine_doubles import (
    ADDRESS,
    AlwaysExpiredSource,
    ExpiredCursorSource,
    ExplodingSession,
    LateArrivalSource,
    MovingWatermarkSource,
    NoDeltaSource,
    RecordingSource,
    build_engine,
    checkpoint,
    message_bytes,
    rows,
    seed_checkpoint,
    source_for,
    target,
)
from mailarc_core.database.entities import MailArchivedMessageEntity
from mailarc_core.mail.errors import MailCursorExpired
from mailarc_core.mail.model import SyncCursorKind
from mailarc_sync.engine.engine import (
    FULL_SCOPE,
    INCREMENTAL_SCOPE,
    PENDING_SCOPE,
)
from mailarc_sync.engine.fake import FakeMailSource
from mailarc_sync.engine.model import ImportCounts, ImportProgress


class TestWhatAFullRunLeavesBehind:
    """A full import has to arm the first delta, or nothing ever will.

    The scheduler only ever asks for incremental runs, so if a full import did
    not leave a starting point the very first delta would bootstrap at today's
    watermark and every message would be somebody else's problem.
    """

    async def test_it_stores_the_watermark_it_started_from(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """A folder's mark is the empty string; the row existing is the point.

        Without it the first scheduled delta would bootstrap instead of
        listing, and `MovingWatermarkSource` further down pins the case where
        the mark is a number that actually moves.
        """
        await make_engine().run(source_for(mailbox), target(account_id))

        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == ""

    async def test_a_message_that_arrives_mid_run_is_the_next_deltas(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """`m4` lands after the last page was listed, so this run never sees it.

        The next delta must, and does. What a *folder* leaves behind cannot
        prove which mark was stored — it has only one — so the sibling above
        pins that half with a mark that moves; this one pins that the message
        actually arrives.
        """
        engine = make_engine()
        await engine.run(LateArrivalSource(mailbox, arrival=4), target(account_id))
        assert (mailbox / "m4.eml").exists()

        source = RecordingSource(mailbox)
        second = await engine.run(
            source, target(account_id), mode=SyncCursorKind.INCREMENTAL
        )

        assert source.fetched == ["m4"]
        assert second.counts.archived == 1

    async def test_a_cancelled_run_leaves_the_watermark_alone(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """It has not covered everything before its mark, so it must replay."""

        async def stop_now() -> bool:
            return True

        await make_engine(batch_size=1).run(
            source_for(mailbox), target(account_id), cancelled=stop_now
        )

        assert await checkpoint(database, account_id, INCREMENTAL_SCOPE) is None

    async def test_a_source_without_deltas_gets_no_starting_point(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """A row here would be a promise the provider cannot keep.

        In either scope: there is no mark to park, so nothing is parked and
        nothing has to be cleared.
        """
        await make_engine().run(NoDeltaSource(mailbox), target(account_id))

        assert await checkpoint(database, account_id, INCREMENTAL_SCOPE) is None
        assert await checkpoint(database, account_id, PENDING_SCOPE) is None
        assert await checkpoint(database, account_id, FULL_SCOPE) is not None


class TestAWalkThatWasInterruptedAndPickedUpAgain:
    """The watermark belongs to the attempt that *began* the walk, not this one.

    A first import of a large mailbox routinely outlives one worker session, so
    resuming is the ordinary path and not an exotic one. `messages.list` is
    newest-first, so a resumed attempt continues *downward* into older mail:
    everything that arrived while the import was interrupted sits above the
    page it resumes at and is in no later page. If the resumed attempt then
    stored *its own* watermark, that mail would be in no delta either — gone
    from the archive for good, with every job reporting success.
    """

    async def test_it_keeps_the_watermark_the_first_attempt_read(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        async def stop_now() -> bool:
            return True

        source = MovingWatermarkSource(mailbox, marks=["h1", "h2", "h3"])
        await make_engine(batch_size=1, checkpoint_every=1).run(
            source, target(account_id), cancelled=stop_now
        )
        assert await checkpoint(database, account_id, FULL_SCOPE) is not None

        await make_engine(batch_size=1, checkpoint_every=1).run(
            source, target(account_id)
        )

        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == "h1", "everything since h1 is still unaccounted for"
        assert source.marks_read == ["h1"], "a resume must not read a fresh one"

    async def test_a_walk_stores_the_mark_it_opened_with_and_not_the_one_it_closes_on(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """A start point sits behind everything the walk fetched; an end point
        sits in front of the mail that arrived while it ran, and loses it."""
        await make_engine(batch_size=1).run(
            MovingWatermarkSource(mailbox, marks=["h1", "h2", "h3"]),
            target(account_id),
        )

        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == "h1"

    async def test_the_pending_mark_is_cleared_once_the_walk_is_through(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """Or the *next* full import would inherit a watermark from this one.

        Two imports of the same account are years apart in practice — a rebuild
        after a graph reset, say — and a leftover mark would arm the delta after
        the second one at the first one's starting point.
        """
        await make_engine().run(
            MovingWatermarkSource(mailbox, marks=["h1"]), target(account_id)
        )

        pending = await checkpoint(database, account_id, PENDING_SCOPE)
        assert pending is None or pending.cursor is None

    async def test_a_walk_that_starts_from_the_top_writes_its_mark_down_first(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """Before the first listing, or an interruption has nothing to inherit."""

        async def stop_now() -> bool:
            return True

        await make_engine(batch_size=1, checkpoint_every=1).run(
            MovingWatermarkSource(mailbox, marks=["h1"]),
            target(account_id),
            cancelled=stop_now,
        )

        pending = await checkpoint(database, account_id, PENDING_SCOPE)
        assert pending is not None
        assert pending.cursor == "h1"

    async def test_an_older_installs_checkpoint_still_gets_a_mark(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """A resume left behind before this scope existed has no mark to inherit.

        Reading a fresh one is the only thing left, and it is what the engine
        did for every resume before — no worse than yesterday, and it happens
        once per account.
        """
        await seed_checkpoint(database, account_id, FULL_SCOPE, "m2")

        source = MovingWatermarkSource(mailbox, marks=["h9"])
        await make_engine().run(source, target(account_id))

        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == "h9"
        assert source.marks_read == ["h9"]


class TestADelta:
    async def test_a_second_run_after_one_new_message_fetches_exactly_that_one(
        self, mailbox, database, account_id, graph, make_engine
    ) -> None:
        """§10, phase 7's definition of done, end to end.

        Three messages imported, one arrives, and the delta *fetches* exactly
        one — which is where the cost is: a fetch is an HTTP round trip, bytes
        on disk and a graph write, while the three it skips cost one batched
        `SELECT` between them. `TestADeltaOverGmail` is the twin where the
        listing is narrow too, because a history walk really can be.
        """
        engine = make_engine()
        await engine.run(source_for(mailbox), target(account_id))
        (mailbox / "m4.eml").write_bytes(message_bytes(4))
        source = RecordingSource(mailbox)

        result = await engine.run(
            source, target(account_id), mode=SyncCursorKind.INCREMENTAL
        )

        assert source.fetched == ["m4"]
        assert result.counts.archived == 1
        assert result.counts.skipped == 3
        assert result.mode is SyncCursorKind.INCREMENTAL
        assert result.resynced is False
        assert graph.messages() == [f"m{number}@example.com" for number in (1, 2, 3, 4)]
        assert len(await rows(database, MailArchivedMessageEntity)) == 4

    async def test_it_moves_a_watermark_that_has_somewhere_to_move_to(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """A delta ends armed for the next one, at the mark it read at its start.

        A folder's mark never moves, so this needs a provider whose does — the
        `historyId` case, where getting it wrong means the next delta starts
        where the last one did and re-lists for ever.
        """
        engine = make_engine()
        await engine.run(source_for(mailbox), target(account_id))
        (mailbox / "m4.eml").write_bytes(message_bytes(4))

        await engine.run(
            MovingWatermarkSource(mailbox, marks=["h7"]),
            target(account_id),
            mode=SyncCursorKind.INCREMENTAL,
        )

        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == "h7"

    async def test_a_delta_with_nothing_new_fetches_nothing_and_says_so(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        engine = make_engine()
        await engine.run(source_for(mailbox), target(account_id))
        source = RecordingSource(mailbox)
        seen: list[ImportProgress] = []

        async def record(progress: ImportProgress) -> None:
            seen.append(progress)

        result = await engine.run(
            source,
            target(account_id),
            mode=SyncCursorKind.INCREMENTAL,
            on_progress=record,
        )

        assert source.fetched == []
        assert result.counts.archived == 0
        assert result.counts.failed == 0
        assert [one.counts.archived for one in seen] == [0, 0], (
            "it still reports — a job row that never moves looks like a hang"
        )

    async def test_it_never_writes_a_full_walks_checkpoint(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """The two scopes hold different alphabets and must not mix.

        One page per message and a checkpoint every one, so a full run would
        have written three of them by the end.
        """
        await seed_checkpoint(database, account_id, INCREMENTAL_SCOPE, "")

        result = await make_engine(batch_size=1, checkpoint_every=1).run(
            source_for(mailbox), target(account_id), mode=SyncCursorKind.INCREMENTAL
        )

        assert result.counts.archived == 3
        assert await checkpoint(database, account_id, FULL_SCOPE) is None


class TestTheFirstDeltaEverRun:
    """There is no history before a watermark, so the first delta is a no-op.

    Listing from `None` instead would walk the whole mailbox under the name of
    a delta — the same messages, an hour of listing, and a progress bar that
    lies about what it is doing.
    """

    async def test_it_fetches_nothing_and_records_where_to_start(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        source = RecordingSource(mailbox)

        result = await make_engine().run(
            source, target(account_id), mode=SyncCursorKind.INCREMENTAL
        )

        assert source.fetched == []
        assert result.counts == ImportCounts()
        assert result.mode is SyncCursorKind.INCREMENTAL
        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == ""
        assert await checkpoint(database, account_id, FULL_SCOPE) is None

    async def test_it_still_reports_progress_once(
        self, mailbox, account_id, make_engine
    ) -> None:
        seen: list[ImportProgress] = []

        async def record(progress: ImportProgress) -> None:
            seen.append(progress)

        await make_engine().run(
            RecordingSource(mailbox),
            target(account_id),
            mode=SyncCursorKind.INCREMENTAL,
            on_progress=record,
        )

        assert [one.counts for one in seen] == [ImportCounts()]

    async def test_a_source_without_deltas_bootstraps_into_nothing(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """No watermark, no row — and so it bootstraps again next time.

        Which is the honest outcome: the scheduler should not be asking this
        provider for deltas at all, and its descriptor says so.
        """
        result = await make_engine().run(
            NoDeltaSource(mailbox), target(account_id), mode=SyncCursorKind.INCREMENTAL
        )

        assert result.counts == ImportCounts()
        assert await checkpoint(database, account_id, INCREMENTAL_SCOPE) is None

    async def test_an_empty_token_is_a_starting_point_and_not_a_missing_one(
        self, tmp_path, database, account_id, make_engine
    ) -> None:
        """The bug a falsy test would have: one message lost per empty mailbox.

        An empty directory watermarks at the empty string, which sorts before
        every file name. Read back as "no checkpoint", the next run would
        bootstrap again — at the newest file this time — and whatever arrived
        in between would never be fetched by anything.
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        engine = make_engine()
        await engine.run(
            FakeMailSource(empty, address=ADDRESS),
            target(account_id),
            mode=SyncCursorKind.INCREMENTAL,
        )
        (empty / "m1.eml").write_bytes(message_bytes(1))
        source = RecordingSource(empty)

        result = await engine.run(
            source, target(account_id), mode=SyncCursorKind.INCREMENTAL
        )

        assert source.fetched == ["m1"]
        assert result.counts.archived == 1


class TestAnExpiredCursor:
    """A cursor the provider will not take is a full walk, not a failed job.

    Gmail keeps a `historyId` usable for about a week and answers a 404 after
    that; the remedy its own documentation names is a full sync. Doing that in
    the same job is what keeps a mailbox that was left alone over the holidays
    from needing a human.
    """

    async def test_it_walks_the_whole_mailbox_in_the_same_run(
        self, mailbox, database, account_id, graph, make_engine
    ) -> None:
        await seed_checkpoint(database, account_id, INCREMENTAL_SCOPE, "42")
        source = ExpiredCursorSource(mailbox)

        result = await make_engine().run(
            source, target(account_id), mode=SyncCursorKind.INCREMENTAL
        )

        assert source.refused == ["42"], "it asked once, was refused, and moved on"
        assert result.resynced is True
        assert result.mode is SyncCursorKind.FULL
        assert result.counts.archived == 3
        assert graph.messages() == [f"m{number}@example.com" for number in (1, 2, 3)]

    async def test_the_resync_archives_nothing_twice(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        """The ledger's unique key would turn a duplicate into a failed job."""
        engine = make_engine()
        await engine.run(source_for(mailbox), target(account_id))
        await seed_checkpoint(database, account_id, INCREMENTAL_SCOPE, "42")

        result = await engine.run(
            ExpiredCursorSource(mailbox),
            target(account_id),
            mode=SyncCursorKind.INCREMENTAL,
        )

        assert result.counts.skipped == 3
        assert result.counts.archived == 0
        assert len(await rows(database, MailArchivedMessageEntity)) == 3

    async def test_it_leaves_a_fresh_watermark_behind(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        await seed_checkpoint(database, account_id, INCREMENTAL_SCOPE, "42")

        await make_engine().run(
            ExpiredCursorSource(mailbox),
            target(account_id),
            mode=SyncCursorKind.INCREMENTAL,
        )

        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == "", "the mark read before the dead cursor was tried"

    async def test_a_resync_that_dies_keeps_the_stale_cursor(
        self, mailbox, database, account_id, tmp_path
    ) -> None:
        """Nothing is cleared on the way in, and that is the whole safety net.

        Clearing the dead cursor and then crashing would leave the next run
        with no checkpoint at all: it would bootstrap at today's watermark and
        everything between the dead cursor and now would be lost. Leaving it
        costs one more refused call and one more full walk.
        """
        await seed_checkpoint(database, account_id, INCREMENTAL_SCOPE, "42")
        engine = build_engine(
            tmp_path=tmp_path, database=database, graph=ExplodingSession()
        )

        with pytest.raises(RuntimeError):
            await engine.run(
                ExpiredCursorSource(mailbox),
                target(account_id),
                mode=SyncCursorKind.INCREMENTAL,
            )

        delta = await checkpoint(database, account_id, INCREMENTAL_SCOPE)
        assert delta is not None
        assert delta.cursor == "42"

    async def test_a_refused_full_walk_ends_the_run(
        self, mailbox, account_id, make_engine
    ) -> None:
        """There is nothing left to fall back to, so it must not loop."""
        source = AlwaysExpiredSource(mailbox)

        with pytest.raises(MailCursorExpired):
            await make_engine().run(source, target(account_id))

        assert source.list_calls == 1

    async def test_a_delta_falls_back_exactly_once(
        self, mailbox, database, account_id, make_engine
    ) -> None:
        await seed_checkpoint(database, account_id, INCREMENTAL_SCOPE, "42")
        source = AlwaysExpiredSource(mailbox)

        with pytest.raises(MailCursorExpired):
            await make_engine().run(
                source, target(account_id), mode=SyncCursorKind.INCREMENTAL
            )

        assert source.list_calls == 2, "the delta, then the walk, then it gave up"
