"""What the worker makes of the two jobs that are about the whole archive.

``derive`` and ``embed`` name no account and open no mailbox, which is what
separates them from the two kinds in ``test_worker.py``: there is no credential
to decrypt, no provider to close and nothing to schedule. What is left is the
bridge — the work happens, its progress reaches the row a page is watching, a
human can stop it, and a failure is left for the loop to classify under §7.6.

The rebuild and the vectors themselves belong to ``mailarc-analytics`` and are
measured there against a real graph.
"""

import asyncio
import threading
from collections.abc import Sequence
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import worker
from mailarc_analytics import (
    DerivedCounts,
    ProgressHook,
    RebuildProgress,
    RebuildStage,
)
from mailarc_analytics.semantic import (
    NO_EMBEDDER,
    CancelCheck,
    EmbedProgress,
    EmbedRun,
    SemanticUnavailable,
)
from mailarc_core.mail.errors import MailTransientError
from mailarc_sync.engine import ProviderRegistry
from mailarc_sync.jobs import JobHandler, JobKind, SessionFactory
from tests.worker_doubles import (
    CancellingQueue,
    RecordingEngine,
    RecordingQueue,
    a_derive_job,
    an_embed_job,
)

ALL_STAGES = tuple(RebuildStage)
"""The five stages a rebuild reports, in the order it reports them."""

FOUND = DerivedCounts(
    messages=31, groups=2, co_addressed=3, topics=1, templates=2, deleted_nodes=5
)
"""What a rebuild hands back — the shape of it, not a measurement."""


class RecordingEmbedding:
    """Stands in for the embed run: it reports the batches it is told to.

    Whether a vector is written, and written once, is settled in
    ``mailarc-analytics`` against a real store. What a job adds is a row and a
    cancel, and both are visible from here — including that the cancel is asked
    *between* batches, since this stub answers the hook and then reads the flag
    exactly as the real loop does.
    """

    def __init__(self, batches: Sequence[tuple[int, int]] = ((1, 0), (2, 1))) -> None:
        self._batches = tuple(batches)
        self.cancels: list[bool] = []
        self.ran = False

    async def __call__(
        self, on_progress: EmbedProgress, cancelled: CancelCheck
    ) -> EmbedRun:
        self.ran = True
        run = EmbedRun(total=3)
        await on_progress(run)
        for done, failed in self._batches:
            run = EmbedRun(total=3, done=done, failed=failed)
            await on_progress(run)
            stop = await cancelled()
            self.cancels.append(stop)
            if stop:
                return run.model_copy(update={"cancelled": True})
        return run


class RecordingRebuild:
    """Stands in for the rebuild: it reports the stages it is told to report.

    Whether the analyses find the planted corpus is settled in
    ``mailarc-analytics`` against a real graph. What a job adds to a rebuild is
    a thread, a row and a cancel, and all three are visible from here.

    Its own per-stage numbers are deliberately nothing like a stage index, so a
    row carrying them instead of the count of finished stages cannot pass.
    """

    def __init__(self, stages: Sequence[RebuildStage] = ALL_STAGES) -> None:
        self._stages = tuple(stages)
        self.reported: list[RebuildStage] = []
        self.thread = threading.current_thread().name
        self.saw_a_loop = True

    def __call__(self, on_progress: ProgressHook) -> DerivedCounts:
        self.thread = threading.current_thread().name
        self.saw_a_loop = _has_a_running_loop()
        for index, stage in enumerate(self._stages, start=1):
            on_progress(RebuildProgress(stage=stage, done=100 * index, total=999))
            self.reported.append(stage)
        return FOUND


def _has_a_running_loop() -> bool:
    """Whether the caller is on an event loop — asked from inside the rebuild."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TestTheDeriveJob:
    """Hours of blocking work on a row the UI is watching, and no mailbox.

    The rebuild itself belongs to ``mailarc-analytics``; what has to be true
    here is that it runs somewhere the web application's loop is not waiting,
    that every stage it finishes reaches the row, and that a human can stop it.
    """

    @staticmethod
    def _handler(
        rebuild: worker.DerivedRebuild, session_factory: SessionFactory | None = None
    ) -> JobHandler:
        def refuse() -> AsyncSession:
            raise AssertionError("a derive job opened a relational session")

        return worker.build_handlers(
            RecordingEngine(),
            ProviderRegistry(),
            session_factory or cast(SessionFactory, refuse),
            rebuild=rebuild,
        )[JobKind.DERIVE]

    async def test_the_rebuild_runs_off_the_event_loop(self) -> None:
        """Every runic driver blocks, and a full read of the archive is not a
        thing to do on the loop that answers the page watching it."""
        rebuild = RecordingRebuild()

        await self._handler(rebuild)(a_derive_job(), RecordingQueue())

        assert rebuild.thread != threading.current_thread().name
        assert not rebuild.saw_a_loop, "the rebuild ran on the event loop"

    async def test_every_finished_stage_moves_the_job_row_on_by_one(self) -> None:
        """Stages, not messages: a rebuild reads the whole archive at every one
        of them, so "messages done" would jump to the total and stay there."""
        queue = RecordingQueue()

        await self._handler(RecordingRebuild())(a_derive_job(job_id=7), queue)

        assert queue.reports == [(7, index, 0, 5) for index in range(1, 6)]

    async def test_a_cancel_between_two_stages_ends_the_rebuild(self) -> None:
        """A job is asked to stop, never killed — and the flag is read in the
        rebuild's own thread, because that is where the next stage starts."""
        rebuild = RecordingRebuild()
        queue = CancellingQueue(after=2)

        await self._handler(rebuild)(a_derive_job(), queue)

        assert rebuild.reported == [RebuildStage.DELETE, RebuildStage.READ]
        assert queue.reports == [(1, 1, 0, 5), (1, 2, 0, 5), (1, 3, 0, 5)], (
            "the stage that was running when the cancel arrived still counts"
        )

    async def test_nothing_on_the_path_asks_which_account(self) -> None:
        """``account_id`` is ``None`` for a job about the whole archive, so a
        derive handler that read one would fail on every job it ever gets."""
        rebuild = RecordingRebuild()

        await self._handler(rebuild)(a_derive_job(), RecordingQueue())

        assert rebuild.reported == list(ALL_STAGES)

    async def test_a_failed_rebuild_is_left_for_the_loop_to_classify(self) -> None:
        """§7.6 is the loop's to act on: swallowing this would end the job as a
        success over half a derived layer."""

        def explode(on_progress: ProgressHook) -> DerivedCounts:
            raise RuntimeError("the graph went away mid-rebuild")

        with pytest.raises(RuntimeError, match="graph went away"):
            await self._handler(explode)(a_derive_job(), RecordingQueue())


class TestTheEmbedJob:
    """The job every "run the embed job" sentence in this application points at.

    The vectors themselves belong to ``mailarc-analytics`` and are tested
    against a real store there. What is only true here is the bridge: the run
    happens, its progress reaches the row the UI polls, a human can stop it,
    and an installation with no embedder configured ends the job with the
    sentence that names the setting instead of with a stack trace.
    """

    @staticmethod
    def _handler(embed: worker.MessageEmbedding) -> JobHandler:
        def refuse() -> AsyncSession:
            raise AssertionError("an embed job opened a relational session")

        return worker.build_handlers(
            RecordingEngine(),
            ProviderRegistry(),
            cast(SessionFactory, refuse),
            embed=embed,
        )[JobKind.EMBED]

    async def test_every_batch_moves_the_job_row_on(self) -> None:
        """Messages, not stages: an embed run really does have a count of
        messages to divide by, which is what ``mail_sync_jobs`` stores."""
        queue = RecordingQueue()

        await self._handler(RecordingEmbedding())(an_embed_job(job_id=9), queue)

        assert queue.reports == [(9, 0, 0, 3), (9, 1, 0, 3), (9, 2, 1, 3)]

    async def test_a_cancel_is_passed_through_to_the_run(self) -> None:
        """The run stops itself between batches; the handler only has to hand
        it the flag off the row, unread."""
        embedding = RecordingEmbedding()
        queue = CancellingQueue(after=1)

        await self._handler(embedding)(an_embed_job(), queue)

        assert embedding.cancels == [False, True]

    async def test_nothing_on_the_path_asks_which_account(self) -> None:
        """An embed job is about the whole archive and names no account."""
        embedding = RecordingEmbedding()

        await self._handler(embedding)(an_embed_job(), RecordingQueue())

        assert embedding.ran

    async def test_with_no_embedder_the_row_carries_the_reason(
        self,
    ) -> None:
        """The state a default installation is in. ``embed_pending`` raises
        this itself; what matters here is that the handler does not swallow it,
        because the job row's error column is where the user reads it."""

        async def unconfigured(
            on_progress: EmbedProgress, cancelled: CancelCheck
        ) -> EmbedRun:
            raise SemanticUnavailable(NO_EMBEDDER)

        with pytest.raises(SemanticUnavailable, match="no embedder is configured"):
            await self._handler(unconfigured)(an_embed_job(), RecordingQueue())

    async def test_a_failed_run_is_left_for_the_loop_to_classify(self) -> None:
        """§7.6 is the loop's to act on — a transient embedder failure has to
        reach it to be retried with backoff."""

        async def explode(
            on_progress: EmbedProgress, cancelled: CancelCheck
        ) -> EmbedRun:
            raise MailTransientError("the model server is warming up")

        with pytest.raises(MailTransientError):
            await self._handler(explode)(an_embed_job(), RecordingQueue())
