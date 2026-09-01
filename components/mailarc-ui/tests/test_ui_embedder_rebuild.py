"""Tests for the vector rebuild control in :mod:`mailarc_ui.embedder.state`.

Against the real :class:`~mailarc_sync.jobs.queue.JobQueue` on the same SQLite
file the form writes its settings to — which is not a convenience but the
point. The control projects a job row and it follows one, and both are only
worth anything if the counters it reads are the ones the queue actually wrote.
Only the clock and Reflex's own state lock are faked, so no test waits.

Three claims live here that no other file can make.

**A second tab must not queue a second run.** The guard is
:meth:`~mailarc_ui.embedder.state.EmbedderSettingsState._adopt_open_embed`, and
it exists because ``job_id`` is per page and starts at zero: a reloaded page
knows about no job. Two embed runs are not destructive the way two derives are,
but they are two workers computing the same vectors — and on ``openai``,
uploading the same archive twice and paying for it.

**A finished run must not revert what somebody is typing.** The form is
editable for the whole minutes-long run, and the poll ends by re-reading the
archive. Re-reading through ``_apply`` — the obvious thing, and what the save
path does — would write the four editable boxes back from the reading and throw
away a half-typed model. ``_recount`` exists for that one reason and this is
where the difference is asserted.

**The bar counts messages.** The insights page's rebuild bar counts stages,
because a derive job moves the row on once per analysis. Rendering an embed run
with that wording would tell somebody watching a 40 000-message archive that
seven units of work exist.
"""

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from embedder_form import (
    Sessions,
    composition,
    configured,
    drive,
    keyed,
    load_form,
    save_form,
    searching,
    sessions,
    state,
)
from sqlalchemy import select

from mailarc_analytics.semantic import SemanticProvider
from mailarc_core.database.entities import MailSyncJobEntity
from mailarc_sync.jobs import JobKind, JobQueue
from mailarc_ui.embedder import (
    EMBED_CANCEL_ASKED,
    EMBED_CANCEL_TOOK_EFFECT,
    EMBED_RUNNING,
    NO_EMBEDDER_TO_RUN,
    UNSAVED_BEFORE_EMBED,
    EmbedderSettingsState,
)

__all__ = ["composition", "configured", "keyed", "searching", "sessions", "state"]
"""pytest collects a fixture off the importing module's namespace, so the five
are imported to be used; ``__all__`` is what stops ruff removing them again —
the same device ``test_ui_insights_rebuild.py`` uses for the same reason."""

STATE_MODULE = "mailarc_ui.embedder.state"

WORKER = "worker-under-test"
LEASE_SECONDS = 60.0


@pytest.fixture
def queue(sessions: Sessions) -> JobQueue:
    """The real queue, on the file the settings live on.

    One database for both on purpose: that is how the application is wired —
    ``mail_sync_jobs`` and ``semantic_settings`` are two tables in the archive's
    own SQLite file — and a test that gave the queue a second file could not
    catch a handler that opened the wrong one.
    """
    return JobQueue(sessions)


@pytest.fixture
def following(
    state: EmbedderSettingsState, queue: JobQueue
) -> Iterator[EmbedderSettingsState]:
    """The form with its queue pointed at our file.

    The patch stays up for the whole test: the state constructs its queue inside
    every handler, which is the production path — a queue built at import would
    capture the world before the application had configured it.
    """
    with patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)):
        state.poll_interval = 0
        yield state


async def _with_an_embedder(state: EmbedderSettingsState) -> None:
    """Load the form and store an embedder, so a rebuild has something to run.

    Through the form's own save rather than by assigning ``in_force``: the
    control reads the *stored* configuration, and a test that faked the reading
    would pass while the button stayed dead in the application.
    """
    await load_form(state)
    await state.set_provider(SemanticProvider.OLLAMA.value)
    await state.set_model("nomic-embed-text")
    await save_form(state)


async def _run_poll(state: EmbedderSettingsState) -> None:
    """Invoke the background handler's underlying coroutine."""
    await drive(EmbedderSettingsState.poll, state)


def _stopping_sleep(state: EmbedderSettingsState, after: int = 1) -> AsyncMock:
    """A sleep that ends the loop, so no test waits on a real clock."""
    calls = {"n": 0}

    async def sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= after:
            state.polling = False

    return AsyncMock(side_effect=sleep)


def _forbidden_sleep() -> AsyncMock:
    """A sleep that fails the test instead of hanging it."""

    async def sleep(_seconds: float) -> None:
        raise AssertionError("the poll slept when it should have stopped")

    return AsyncMock(side_effect=sleep)


async def _job_count(queue: JobQueue) -> int:
    """How many rows the queue holds, asked the way a test may ask it."""
    async with queue._session_factory() as session:  # noqa: SLF001 - a test's reach
        result = await session.execute(select(MailSyncJobEntity.id))
        return len(result.scalars().all())


class TestStartingARebuild:
    async def test_it_is_one_embed_job_about_the_whole_archive(
        self, following, queue, searching
    ) -> None:
        await _with_an_embedder(following)

        result = await following.start_embed()

        job = await queue.get(following.job_id)
        assert job is not None
        assert job.kind == JobKind.EMBED
        assert job.account_id is None
        assert following.job.status == "queued"
        assert following.job.active is True
        assert following.can_embed is False
        assert following.polling is True
        assert result is EmbedderSettingsState.poll

    async def test_a_running_rebuild_is_not_started_twice(
        self, following, searching
    ) -> None:
        await _with_an_embedder(following)
        await following.start_embed()
        first = following.job_id

        assert await following.start_embed() is None
        assert following.job_id == first
        assert following.embed_message == EMBED_RUNNING

    async def test_a_finished_rebuild_no_longer_blocks_the_next_one(
        self, following, queue, searching
    ) -> None:
        """The control may be stale; it must re-read before it refuses."""
        await _with_an_embedder(following)
        await following.start_embed()
        first = following.job_id
        await queue.succeed(first)

        assert await following.start_embed() is None  # already polling
        assert following.job_id != first
        assert following.job.active is True

    async def test_a_reloaded_page_follows_the_run_instead_of_queueing(
        self, following, queue, searching
    ) -> None:
        """Two open tabs, or one tab after a reload, would queue two.

        ``job_id`` starts at zero, so a freshly loaded page knows about no run
        and would enqueue its own — two workers computing the same vectors and,
        on ``openai``, uploading the same archive twice.
        """
        await _with_an_embedder(following)
        await following.start_embed()
        first = following.job_id

        with patch(f"{STATE_MODULE}.JobQueue", Mock(return_value=queue)):
            second_tab = EmbedderSettingsState()
            second_tab.poll_interval = 0
            await load_form(second_tab)
            assert await second_tab.start_embed() is not None

        assert second_tab.job_id == first
        assert second_tab.embed_message == EMBED_RUNNING
        assert await _job_count(queue) == 1

    async def test_a_queue_that_refuses_leaves_a_message_not_a_traceback(
        self, following, queue, searching
    ) -> None:
        await _with_an_embedder(following)

        with patch.object(
            queue, "enqueue", AsyncMock(side_effect=RuntimeError("db went away"))
        ):
            assert await following.start_embed() is None

        assert "db went away" in following.embed_message
        assert following.starting is False
        assert following.polling is False

    async def test_a_queue_that_cannot_be_asked_still_queues_the_rebuild(
        self, following, queue, searching
    ) -> None:
        """Refusing because the *lookup* failed would be the wrong way to fail:
        the enqueue immediately after goes to the same database and already has
        the message for it."""
        await _with_an_embedder(following)

        with patch.object(
            queue, "find_open", AsyncMock(side_effect=RuntimeError("db went away"))
        ):
            assert await following.start_embed() is not None

        assert following.job_id > 0
        assert following.job.active is True


class TestWhatTheControlRefusesToStart:
    async def test_no_embedder_means_no_job_and_a_sentence(
        self, following, queue, searching
    ) -> None:
        """A default installation. The job would raise ``SemanticUnavailable``
        with a good message; it would simply arrive minutes later, in a job
        list nobody is looking at."""
        await load_form(following)
        assert following.embedder_configured is False
        assert following.can_embed is False

        assert await following.start_embed() is None

        assert following.embed_message == NO_EMBEDDER_TO_RUN
        assert await _job_count(queue) == 0

    async def test_unsaved_changes_are_named_before_the_run(
        self, following, searching
    ) -> None:
        """The trap this control creates: the worker embeds with what is
        *stored*, and the button sits directly under a warning telling somebody
        to change the model."""
        await _with_an_embedder(following)
        await following.set_model("text-embedding-3-small")
        assert following.settings_unsaved is True

        await following.start_embed()

        assert following.embed_message == UNSAVED_BEFORE_EMBED
        assert following.job.active is True

    async def test_a_saved_form_starts_without_that_warning(
        self, following, searching
    ) -> None:
        await _with_an_embedder(following)
        assert following.settings_unsaved is False

        await following.start_embed()

        assert following.embed_message == ""

    async def test_whitespace_alone_is_not_an_unsaved_change(
        self, following, searching
    ) -> None:
        """A save stores the stripped value, so trailing space is not a
        difference and must not raise a warning about one."""
        await _with_an_embedder(following)

        await following.set_model("nomic-embed-text  ")

        assert following.settings_unsaved is False


class TestCancelling:
    async def test_cancel_asks_and_the_buttons_follow(
        self, following, queue, searching
    ) -> None:
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await following._sync_job()  # noqa: SLF001 - the control's own read
        assert following.can_cancel_embed is True

        await following.cancel_embed()

        assert await queue.is_cancel_requested(following.job_id) is True
        assert following.job.cancel_requested is True
        assert following.job.status == "running"  # a flag, not a kill
        assert following.can_cancel_embed is False
        assert following.cancelling is False
        assert following.embed_message == EMBED_CANCEL_ASKED

    async def test_cancelling_before_a_worker_arrives_gives_the_control_back(
        self, following, queue, searching
    ) -> None:
        """``request_cancel`` ends an unclaimed job outright rather than
        flagging it, so the button is live again and the message has to say why
        instead of promising a worker that never came."""
        await _with_an_embedder(following)
        await following.start_embed()

        await following.cancel_embed()

        assert following.job.status == "cancelled"
        assert following.can_embed is True
        assert following.can_cancel_embed is False
        assert following.embed_message == EMBED_CANCEL_TOOK_EFFECT

    async def test_cancelling_a_run_that_already_ended_says_so(
        self, following, queue, searching
    ) -> None:
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(following.job_id)

        await following.cancel_embed()

        assert following.embed_message == "That rebuild had already ended."

    async def test_cancel_without_a_run_is_a_no_op(self, following, searching) -> None:
        await _with_an_embedder(following)

        await following.cancel_embed()

        assert following.embed_message == ""
        assert following.has_embed_job is False


class TestTheBarCountsMessages:
    async def test_it_reports_messages_and_not_stages(
        self, following, queue, searching
    ) -> None:
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(following.job_id, done=300, failed=0, total=1200)

        await following._sync_job()  # noqa: SLF001 - the control's own read

        assert following.job.status == "running"
        assert following.job.percent_label == "25%"
        assert following.job.messages_label == "300 of 1200 messages"
        assert "stage" not in following.job.messages_label

    async def test_a_run_that_has_not_reported_yet_shows_no_percentage(
        self, following, searching
    ) -> None:
        await _with_an_embedder(following)

        await following.start_embed()

        assert following.job.percent_label == "—"
        assert following.job.messages_label == ""

    async def test_refusals_are_named_beside_the_count(
        self, following, queue, searching
    ) -> None:
        """The number that explains a finished run whose coverage is still
        short: a batch the provider refused permanently leaves messages with no
        vector and no further job to fix them until somebody looks."""
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(following.job_id, done=90, failed=10, total=100)

        await following._sync_job()  # noqa: SLF001 - the control's own read

        assert following.job.messages_label == "90 of 100 messages · 10 refused"
        assert following.job.percent_label == "100%"


class TestFollowingTheRun:
    async def test_the_poll_picks_up_a_batch_that_finished(
        self, following, queue, searching
    ) -> None:
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.progress(following.job_id, done=64, failed=0, total=256)
        sleep = _stopping_sleep(following)

        with patch.object(asyncio, "sleep", sleep):
            await _run_poll(following)

        assert following.job.messages_label == "64 of 256 messages"
        assert sleep.await_count == 1

    async def test_a_finished_run_stops_the_poll_and_recounts(
        self, following, queue, searching
    ) -> None:
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(following.job_id)
        searching.embedded = 1200

        with patch.object(asyncio, "sleep", _forbidden_sleep()):
            await _run_poll(following)

        assert following.polling is False
        assert following.job.status == "succeeded"
        assert following.in_force.embedded == 1200

    async def test_a_cancelled_run_recounts_too(
        self, following, queue, searching
    ) -> None:
        """It embedded everything up to the batch it stopped on, and the
        warnings above compare against that number."""
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.cancel(following.job_id)
        searching.embedded = 640

        with patch.object(asyncio, "sleep", _forbidden_sleep()):
            await _run_poll(following)

        assert following.job.status == "cancelled"
        assert following.in_force.embedded == 640

    async def test_a_finished_run_does_not_revert_a_half_typed_model(
        self, following, queue, searching
    ) -> None:
        """The reason ``_recount`` exists instead of ``_apply``.

        A rebuild takes minutes and the form is editable throughout. Refreshing
        through ``_apply`` — which is right after a save, and is what that path
        does — writes the four editable boxes back from the reading, so a job
        finishing would silently throw away the model somebody is halfway
        through changing.
        """
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(following.job_id)
        await following.set_model("text-embedding-3-small")
        await following.set_dimension(1536)
        searching.embedded = 1200

        with patch.object(asyncio, "sleep", _forbidden_sleep()):
            await _run_poll(following)

        assert following.model == "text-embedding-3-small"
        assert following.dimension == 1536
        assert following.settings_unsaved is True
        assert following.in_force.embedded == 1200  # the count did refresh

    async def test_a_dropped_read_is_not_an_end(
        self, following, queue, searching
    ) -> None:
        """The one thing that is not an exit from the loop: the next tick asks
        again, and the control keeps showing the last reading."""
        await _with_an_embedder(following)
        await following.start_embed()
        sleep = _stopping_sleep(following)

        with (
            patch.object(asyncio, "sleep", sleep),
            patch.object(queue, "get", AsyncMock(side_effect=RuntimeError("hiccup"))),
        ):
            await _run_poll(following)

        assert following.job.active is True
        assert following.job_id > 0
        assert sleep.await_count == 1

    async def test_the_poll_gives_up_on_a_run_nobody_picks_up(
        self, following, queue, searching
    ) -> None:
        """A loop whose only exit is a final state never ends without a worker,
        which is the normal state of a dev machine."""
        await _with_an_embedder(following)
        await following.start_embed()
        following.poll_ticks_allowed = 3
        sleep = AsyncMock()

        with patch.object(asyncio, "sleep", sleep):
            await _run_poll(following)

        assert following.polling is False
        assert following.job.status == "queued"
        assert sleep.await_count == 3
        assert "no worker" in following.embed_message

    async def test_a_running_job_gets_a_different_sentence(
        self, following, queue, searching
    ) -> None:
        """Giving up is a statement about this page, never about the job.

        A run a worker really is doing has not gone wrong, and telling that
        reader to start a worker would be wrong twice over.
        """
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        following.poll_ticks_allowed = 1

        with patch.object(asyncio, "sleep", AsyncMock()):
            await _run_poll(following)

        assert following.job.status == "running"
        assert "no worker" not in following.embed_message
        assert "Reload to see where it got to" in following.embed_message

    async def test_a_poll_stopped_mid_read_applies_nothing(
        self, following, queue, searching
    ) -> None:
        """The page went away while the read was out.

        The read happens off the lock, so ``stop_polling`` can land between
        asking for the row and applying it. Applying it anyway would write to a
        state whose page is gone, and — worse — go on to the recount.
        """
        await _with_an_embedder(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(following.job_id)
        before = following.job.status

        async def read_then_navigate_away(job_id: int) -> object:
            following.polling = False
            return await EmbedderSettingsState._read_job(following, job_id)  # noqa: SLF001

        with (
            patch.object(asyncio, "sleep", _forbidden_sleep()),
            patch.object(
                EmbedderSettingsState,
                "_read_job",
                AsyncMock(side_effect=read_then_navigate_away),
            ),
        ):
            await _run_poll(following)

        assert following.job.status == before  # nothing was applied
        assert following.polling is False

    async def test_a_recount_that_fails_does_not_escape_the_poll(
        self, following, queue, searching
    ) -> None:
        """A graph that went away while the rebuild ran is not a traceback out
        of a background task — it leaves the last count standing."""
        await _with_an_embedder(following)
        searching.embedded = 40
        await load_form(following)
        await following.start_embed()
        await queue.claim(WORKER, LEASE_SECONDS)
        await queue.succeed(following.job_id)

        with (
            patch.object(asyncio, "sleep", _forbidden_sleep()),
            patch.object(
                EmbedderSettingsState,
                "_read",
                AsyncMock(side_effect=ConnectionError("the graph went away")),
            ),
        ):
            await _run_poll(following)

        assert following.job.status == "succeeded"
        assert following.in_force.embedded == 40

    async def test_a_job_read_that_fails_leaves_the_last_reading_standing(
        self, following, queue, searching
    ) -> None:
        """A database hiccup is not news that the rebuild ended, and a button
        handler is not a place to raise from."""
        await _with_an_embedder(following)
        await following.start_embed()
        watched = following.job_id

        with patch.object(
            queue, "get", AsyncMock(side_effect=RuntimeError("db went away"))
        ):
            assert await following.start_embed() is None

        assert following.job_id == watched
        assert following.job.active is True
        assert following.embed_message == EMBED_RUNNING

    def test_stop_polling_clears_the_flag(self, following) -> None:
        following.polling = True

        following.stop_polling()

        assert following.polling is False


class TestTheControlOnAPageThatCannotRead:
    async def test_a_graph_that_does_not_answer_still_offers_the_button(
        self, following, searching
    ) -> None:
        """Configuring an embedder is something you do *before* it works, and
        the count is the only read here allowed to fail quietly."""
        searching.error = ConnectionError("the graph is not running")
        await _with_an_embedder(following)

        assert following.in_force.coverage_known is False
        assert following.embedder_configured is True
        assert following.can_embed is True
