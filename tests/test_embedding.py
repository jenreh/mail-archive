"""``python -m app.embedding``: one embed run against the configured archive.

What a vector *is*, whether a page is walked once and whether a cancel is
honoured are settled in ``mailarc-analytics`` against a real FalkorDB. What is
only true here is the half this module adds: that the run is pointed at *this
installation's* graph, *this installation's* embedder and *this installation's*
settings, that the session is opened per graph call rather than held, that the
two hooks a job needs reach the run unchanged, and that a shell task can tell
from the exit status whether it worked.

The embedder-off case gets a test of its own because it is the state every
default installation is in. It has to end the command as a *failure* — somebody
who typed `task graph:embed` asked for vectors, and reporting success over
nothing would be a lie — carrying the sentence that names the setting to change.
"""

import contextlib
import logging
from collections.abc import Iterator

import pytest

from app import composition, embedding
from mailarc_analytics.semantic import (
    NO_EMBEDDER,
    CancelCheck,
    EmbedProgress,
    EmbedRun,
    SemanticConfig,
    SemanticUnavailable,
)
from mailarc_core import GraphConfig
from mailarc_core.archive.reader import GraphSessionFactory

DONE = EmbedRun(total=9, done=7, failed=2)
"""Three counts with three different values, so a transposed pair shows."""


class StubEmbedder:
    """An embedder that is never called — only asked what it is."""

    model = "stub-model"
    dimension = 4


class Recorder:
    """An embed run that says what it was pointed at, and reports as it goes."""

    def __init__(self, run: EmbedRun = DONE) -> None:
        self.run = run
        self.factories: list[GraphSessionFactory] = []
        self.embedders: list[object] = []
        self.configs: list[SemanticConfig] = []
        self.hooks: list[EmbedProgress | None] = []
        self.cancels: list[CancelCheck | None] = []
        self.open_sessions = 0
        self.sessions_opened = 0

    @contextlib.contextmanager
    def graph_session(self, config: GraphConfig) -> Iterator[str]:
        self.graph_config = config
        self.open_sessions += 1
        self.sessions_opened += 1
        try:
            yield "the session"
        finally:
            self.open_sessions -= 1

    async def embed_pending(
        self,
        graph_session: GraphSessionFactory,
        embedder: object,
        config: SemanticConfig | None = None,
        *,
        on_progress: EmbedProgress | None = None,
        cancelled: CancelCheck | None = None,
    ) -> EmbedRun:
        self.factories.append(graph_session)
        self.embedders.append(embedder)
        assert config is not None
        self.configs.append(config)
        self.hooks.append(on_progress)
        self.cancels.append(cancelled)
        # The real one opens a session per graph call and closes it again; this
        # imitates that so the assertion about *when* one is open is about the
        # factory this module hands down rather than about a stub's manners.
        with graph_session():
            self.sessions_while_running = self.open_sessions
        return self.run


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """``app.embedding`` with the graph and the run swapped for a witness."""
    recording = Recorder()
    monkeypatch.setattr(embedding, "graph_session", recording.graph_session)
    monkeypatch.setattr(embedding, "embed_pending", recording.embed_pending)
    monkeypatch.setattr(embedding, "semantic_embedder", StubEmbedder)
    return recording


async def test_the_run_is_pointed_at_the_configured_archive(
    recorder: Recorder,
) -> None:
    """A component knows how to embed, not which graph, which model or how
    large a page is — which is why this module exists at all."""
    await embedding.embed()

    assert recorder.graph_config is composition.graph_config()
    assert recorder.configs == [composition.semantic_config()]
    assert isinstance(recorder.embedders[0], StubEmbedder)


async def test_a_session_is_opened_per_graph_call_and_not_held(
    recorder: Recorder,
) -> None:
    """A factory, not a session. An embed run is mostly waiting on somebody
    else's HTTP server, so a driver held for the length of it would be a
    connection idle for most of the job's life."""
    await embedding.embed()

    assert recorder.sessions_while_running == 1
    assert recorder.open_sessions == 0
    assert recorder.sessions_opened == 1, "nothing opened one before the run"


async def test_both_hooks_reach_the_run_unchanged(recorder: Recorder) -> None:
    """The job's whole way of moving its row and of being stopped."""
    seen: list[EmbedRun] = []

    async def watch(run: EmbedRun) -> None:
        seen.append(run)

    async def stop() -> bool:
        return True

    await embedding.embed(watch, stop)

    hook, cancel = recorder.hooks[0], recorder.cancels[0]
    assert hook is watch
    assert cancel is stop
    assert hook is not None
    await hook(DONE)
    assert seen == [DONE]


async def test_a_run_with_no_hooks_is_allowed(recorder: Recorder) -> None:
    """``task graph:embed`` has no row to report into and nobody to ask."""
    assert await embedding.embed() == DONE

    assert recorder.hooks == [None]
    assert recorder.cancels == [None]


async def test_what_the_run_did_reaches_the_log(
    recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """The command prints nothing, so this line is the whole of what a person
    sees of a run started from a shell."""
    with caplog.at_level(logging.INFO, logger="app.embedding"):
        await embedding.embed()

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "7 of 9" in logged
    assert "2 could not be embedded" in logged
    assert "finished" in logged


async def test_a_cancelled_run_says_so_rather_than_reporting_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Seven of nine embedded is a different thing from seven of seven, and a
    log line that could not tell them apart would be worse than none."""
    stopped = Recorder(DONE.model_copy(update={"cancelled": True}))
    monkeypatch.setattr(embedding, "graph_session", stopped.graph_session)
    monkeypatch.setattr(embedding, "embed_pending", stopped.embed_pending)
    monkeypatch.setattr(embedding, "semantic_embedder", StubEmbedder)

    with caplog.at_level(logging.INFO, logger="app.embedding"):
        await embedding.embed()

    assert "cancelled" in caplog.text


def test_a_finished_run_exits_zero(recorder: Recorder) -> None:
    assert embedding.main() == 0


def test_the_command_prints_nothing(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only one of the two callers has a stdout; the log has both."""
    embedding.main()

    assert capsys.readouterr().out == ""


def test_with_no_embedder_the_command_fails_and_says_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The state every default installation is in. Somebody who typed the
    command asked for vectors, so "nothing to do" would be a lie — and the
    reason has to survive into the log, because a shell task has nowhere else
    to put it. The log carries the component's own sentence rather than a
    paraphrase, which is what stops the command and the pages disagreeing
    about the same state."""
    monkeypatch.setattr(embedding, "semantic_embedder", lambda: None)

    with caplog.at_level(logging.ERROR, logger="app.embedding"):
        assert embedding.main() == 1

    assert "could not be embedded" in caplog.text
    assert NO_EMBEDDER in caplog.text


async def test_the_no_embedder_refusal_comes_from_the_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not re-raised here. ``embed_pending`` takes ``embedder | None`` on
    purpose so the refusal happens once, in one place, with one wording — this
    module passing ``None`` straight through is what keeps that true."""
    monkeypatch.setattr(embedding, "semantic_embedder", lambda: None)

    with pytest.raises(SemanticUnavailable) as caught:
        await embedding.embed()

    assert str(caught.value) == NO_EMBEDDER


def test_a_failed_run_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``task graph:embed`` can only act on the status; the traceback is for
    the person reading afterwards."""

    async def explode(
        on_progress: EmbedProgress | None = None,
        cancelled: CancelCheck | None = None,
    ) -> EmbedRun:
        raise RuntimeError("the model server is not running")

    monkeypatch.setattr(embedding, "embed", explode)

    with caplog.at_level(logging.ERROR, logger="app.embedding"):
        assert embedding.main() == 1

    assert "could not be embedded" in caplog.text
    assert "not running" in caplog.text, "the reason has to survive into the log"


def test_the_worker_runs_this_and_nothing_else(recorder: Recorder) -> None:
    """The handler's default, asserted rather than assumed: an embed job that
    called something else would be a second way to configure an embedder."""
    from app import worker

    parameters = worker.build_handlers.__defaults__ or ()

    assert embedding.embed in parameters


def test_the_command_adopts_the_stored_settings_before_it_embeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remedy a user is sent to run must obey the settings they just saved.

    ``main`` is its own process with its own composition root, so
    ``_semantic_override`` is ``None`` in it until something reads the row.
    Without this the command embeds with the *file's* provider and model while
    the pages search with the stored one, writes ``embedding_model`` from the
    wrong space, and reports "finished, N written" — after which the search
    still answers ``nothing_embedded``, because the KNN filters on that field.
    Ordering matters as much as the call: adopting after the run would leave
    the run reading the configuration it was supposed to replace.
    """
    order: list[str] = []

    async def adopting() -> None:
        order.append("adopt")

    async def running(
        on_progress: EmbedProgress | None = None,
        cancelled: CancelCheck | None = None,
    ) -> EmbedRun:
        order.append("embed")
        return DONE

    monkeypatch.setattr(embedding, "adopt_semantic_settings", adopting)
    monkeypatch.setattr(embedding, "embed", running)

    assert embedding.main() == 0
    assert order == ["adopt", "embed"]
