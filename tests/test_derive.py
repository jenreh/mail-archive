"""``python -m app.derive``: one rebuild against the configured graph.

What the analyses find is settled in ``mailarc-analytics`` against a real
FalkorDB. What is only true here is the half this module adds — that the
rebuild is handed *this installation's* graph and thresholds, that the session
is let go of afterwards, that a shell task can tell from the exit status
whether it worked, and that all fourteen counts reach the log.

That last one earns a fixture of its own. Nothing enqueues a ``derive`` job
yet, so these two log lines are the whole of what a person ever sees of a
rebuild, and a count that fell out of them or swapped places with its
neighbour is invisible everywhere else.
"""

import contextlib
import logging
import re
from collections.abc import Iterator, Sequence

import pytest

from app import composition, derive
from mailarc_analytics import (
    AnalyticsConfig,
    DerivedCounts,
    ProgressHook,
    RebuildProgress,
    RebuildStage,
)
from mailarc_analytics.derived.model import EMBEDDING_METHOD, SimilarityEdge
from mailarc_analytics.semantic import SemanticConfig, SemanticProvider, SimilarPair
from mailarc_core import GraphConfig

FOUND = DerivedCounts(
    messages=31,
    beyond_ceiling=12,
    unidentified=1,
    groups=2,
    co_addressed=3,
    wide_messages=4,
    topics=5,
    dropped_buckets=6,
    dropped_weak_pairs=7,
    templates=8,
    unhashable_messages=9,
    dropped_template_buckets=10,
    deleted_nodes=11,
    deleted_edges=13,
)
"""Fourteen counts with fourteen genuinely different values.

Different because equal ones make the test below unable to see the mistake it
exists for: two fields sharing a value can be swapped in the format string and
the rendered line still reads as right.
"""

PHRASES = {
    "messages": "messages:",
    "beyond_ceiling": "messages beyond the ceiling",
    "unidentified": "with no canonical id",
    "groups": "groups",
    "co_addressed": "co-addressed pairs",
    "wide_messages": "addressed too widely",
    "topics": "topics",
    "dropped_buckets": "signal buckets",
    "dropped_weak_pairs": "weak pairs",
    "templates": "templates",
    "unhashable_messages": "with no fingerprint",
    "dropped_template_buckets": "band buckets",
    "deleted_nodes": "nodes",
    "deleted_edges": "edges",
}
"""What each count is called where it is logged.

The words are the point. Searching the joined log for the bare number proves
only that it was printed somewhere, which two counts of equal value satisfy
between them and which a format string with two arguments transposed satisfies
as well.
"""


class Recorder:
    """A rebuild that says which graph it was pointed at, and stays open."""

    def __init__(self, counts: DerivedCounts = FOUND) -> None:
        self.counts = counts
        self.sessions: list[object] = []
        self.configs: list[AnalyticsConfig] = []
        self.hooks: list[ProgressHook | None] = []
        self.extra_edges: list[tuple[SimilarityEdge, ...]] = []
        self.open_sessions = 0

    @contextlib.contextmanager
    def graph_session(self, config: GraphConfig) -> Iterator[str]:
        self.graph_config = config
        self.open_sessions += 1
        try:
            yield "the session"
        finally:
            self.open_sessions -= 1

    def rebuild_derived(
        self,
        session: object,
        config: AnalyticsConfig,
        *,
        on_progress: ProgressHook | None = None,
        extra_edges: Sequence[SimilarityEdge] = (),
    ) -> DerivedCounts:
        self.sessions.append(session)
        self.configs.append(config)
        self.hooks.append(on_progress)
        self.extra_edges.append(tuple(extra_edges))
        self.session_was_open = self.open_sessions
        return self.counts


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """``app.derive`` with the graph and the analysis swapped for a witness."""
    recording = Recorder()
    monkeypatch.setattr(derive, "graph_session", recording.graph_session)
    monkeypatch.setattr(derive, "rebuild_derived", recording.rebuild_derived)
    return recording


def test_the_rebuild_runs_against_the_configured_graph(recorder: Recorder) -> None:
    """A component knows how to rebuild, not which graph — that is why this
    module exists at all."""
    counts = derive.rebuild()

    assert recorder.graph_config is composition.graph_config()
    assert recorder.configs == [composition.analytics_config()]
    assert recorder.sessions == ["the session"]
    assert counts == FOUND


def test_the_session_is_open_while_the_rebuild_runs_and_shut_after(
    recorder: Recorder,
) -> None:
    """A rebuild is a command, not a service: nothing holds a driver open
    between two of them."""
    derive.rebuild()

    assert recorder.session_was_open == 1
    assert recorder.open_sessions == 0


def test_the_progress_hook_reaches_the_rebuild(recorder: Recorder) -> None:
    """The ``derive`` job's whole way of moving its row."""
    seen: list[RebuildProgress] = []
    hook = seen.append

    derive.rebuild(hook)

    recorded = recorder.hooks[0]
    assert recorded is hook
    assert recorded is not None

    recorded(RebuildProgress(stage=RebuildStage.READ, done=31, total=32))
    assert seen[0].stage is RebuildStage.READ


def test_every_count_has_a_phrase_it_is_logged_behind() -> None:
    """A fifteenth count is a visible edit here rather than a silent omission
    from the only place a person reads these numbers."""
    assert set(FOUND.model_dump()) == set(PHRASES)


def test_the_fourteen_values_are_fourteen_different_numbers() -> None:
    """Two equal ones can be transposed in the format string and the line still
    reads as right, which is exactly the mistake the test below looks for."""
    assert len(set(FOUND.model_dump().values())) == len(PHRASES)


def test_every_count_is_logged_under_its_own_name(
    recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    """A rebuild that dropped four hundred messages and a rebuild that found
    nothing to say look the same in a graph; they must not in a log.

    Matched against the rendered phrase rather than against the bare number, so
    that two arguments swapped in the format string is a failure and not a
    coincidence. The lookbehind is what stops ``1 with no canonical id``
    matching inside ``31 with no canonical id``.
    """
    with caplog.at_level(logging.INFO, logger="app.derive"):
        derive.rebuild()

    logged = " ".join(record.getMessage() for record in caplog.records)
    for name, value in FOUND.model_dump().items():
        pattern = rf"(?<!\d){value} {re.escape(PHRASES[name])}"
        assert re.search(pattern, logged), (
            f"{name} = {value} is not logged as {PHRASES[name]!r}"
        )


def test_a_finished_rebuild_exits_zero(recorder: Recorder) -> None:
    assert derive.main() == 0


def test_a_failed_rebuild_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``task graph:rebuild-derived`` can only act on the status; the traceback
    is for the person reading afterwards."""

    def explode(on_progress: ProgressHook | None = None) -> DerivedCounts:
        raise RuntimeError("the graph server is not running")

    monkeypatch.setattr(derive, "rebuild", explode)

    with caplog.at_level(logging.ERROR, logger="app.derive"):
        assert derive.main() == 1

    assert "could not be rebuilt" in caplog.text
    assert "not running" in caplog.text, "the reason has to survive into the log"


def test_the_command_prints_nothing(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only one of the two callers has a stdout; the log has both."""
    derive.main()

    assert capsys.readouterr().out == ""


class TestSignalSix:
    """The seam §10 phase 6 item 3 asks for, and the one that had no caller.

    ``build_topics`` grew its sixth signal, ``SemanticConfig`` grew the
    threshold that gates it, and both were unreachable: nothing computed a
    neighbour and ``rebuild_derived`` had no parameter to forward one through.
    So this module — the only layer allowed to name both ``derived`` and
    ``semantic`` — is where the two are joined, and these are the tests that
    say the joint is really there.
    """

    def test_with_no_embedder_the_rebuild_is_exactly_the_phase_five_one(
        self, recorder: Recorder
    ) -> None:
        """§7.4's default, and the DoD's "all phase-5 analyses run unchanged".
        No graph is read for neighbours and no edge is offered."""
        derive.rebuild()

        assert recorder.extra_edges == [()]

    def test_a_configured_embedder_turns_neighbours_into_edges(
        self, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pairs arrive as similarities and leave as suggestions, labelled
        ``embedding`` so that ``ABOUT.method`` still tells a reader which of the
        six signals drew the edge."""
        _with_embedder(monkeypatch, SemanticConfig(provider=SemanticProvider.OLLAMA))
        monkeypatch.setattr(
            derive,
            "similar_pairs",
            lambda *args, **kwargs: (SimilarPair(left="a", right="b", score=0.91),),
        )

        derive.rebuild()

        assert recorder.extra_edges == [
            (SimilarityEdge(left="a", right="b", method=EMBEDDING_METHOD, weight=0.91),)
        ]

    def test_the_threshold_and_the_model_reach_the_read(
        self, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``topic_similarity_min`` was read by nothing at all. 0.82 is high on
        purpose, and a rebuild that ignored it would join half the archive."""
        settings = SemanticConfig(
            provider=SemanticProvider.OLLAMA, topic_similarity_min=0.91
        )
        embedder = _with_embedder(monkeypatch, settings)
        asked: list[dict[str, object]] = []

        def record(session: object, **kwargs: object) -> tuple[SimilarPair, ...]:
            asked.append(kwargs)
            return ()

        monkeypatch.setattr(derive, "similar_pairs", record)

        derive.rebuild()

        assert asked[0]["minimum"] == 0.91
        assert asked[0]["model"] == embedder.model
        assert asked[0]["neighbours"] == settings.topic_neighbours

    def test_the_neighbour_read_shares_the_rebuilds_session(
        self, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One session for the whole command: two would let the neighbours and
        the facts come from two different instants of a live archive."""
        _with_embedder(monkeypatch, SemanticConfig(provider=SemanticProvider.OLLAMA))
        seen: list[object] = []

        def record(session: object, **kwargs: object) -> tuple[SimilarPair, ...]:
            seen.append(session)
            return ()

        monkeypatch.setattr(derive, "similar_pairs", record)

        derive.rebuild()

        assert seen == recorder.sessions

    def test_a_neighbour_read_that_fails_does_not_cost_the_rebuild(
        self,
        recorder: Recorder,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Signal 6 is the one signal that is a suggestion. Losing it costs a
        few topic memberships; losing the rebuild costs the whole derived
        layer, and an un-upgraded graph with no vector index is exactly the
        state that would do it."""
        _with_embedder(monkeypatch, SemanticConfig(provider=SemanticProvider.OLLAMA))

        def explode(session: object, **kwargs: object) -> tuple[SimilarPair, ...]:
            raise RuntimeError("no vector index on Message.embedding")

        monkeypatch.setattr(derive, "similar_pairs", explode)

        with caplog.at_level(logging.WARNING, logger="app.derive"):
            counts = derive.rebuild()

        assert counts == FOUND
        assert recorder.extra_edges == [()]
        assert "vector index" in caplog.text


def _with_embedder(
    monkeypatch: pytest.MonkeyPatch, settings: SemanticConfig
) -> _StubEmbedder:
    """Point ``app.derive`` at a configured embedder without building one."""

    embedder = _StubEmbedder()
    monkeypatch.setattr(derive, "semantic_config", lambda: settings)
    monkeypatch.setattr(derive, "semantic_embedder", lambda: embedder)
    return embedder


class _StubEmbedder:
    """An embedder that is only ever asked its model's name.

    Nothing here embeds: ``app.derive`` reads ``embedder.model`` to say which
    space it is asking the neighbour read about, and never calls it.
    """

    model = "stub-model"
    dimension = 4


def test_the_command_adopts_the_stored_settings_before_it_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signal 6 is read through the embedder, so a stale one degrades it silently.

    ``_semantic_edges`` asks :func:`app.composition.semantic_embedder` and
    :func:`app.composition.semantic_config`, and in a fresh command process
    neither has seen the stored row. A file ``provider: none`` under a stored
    ``ollama`` then returns ``()`` and A2 runs on five signals with no error
    anywhere; a file model under a stored one asks the graph for neighbours in
    a space the archive was not embedded in and gets none back. Both write a
    derived layer that differs from the one the worker builds from the same
    archive, and the insights page reads it.
    """
    order: list[str] = []

    async def adopting() -> None:
        order.append("adopt")

    def rebuilding(on_progress: ProgressHook | None = None) -> DerivedCounts:
        order.append("rebuild")
        return FOUND

    monkeypatch.setattr(derive, "adopt_semantic_settings", adopting)
    monkeypatch.setattr(derive, "rebuild", rebuilding)

    assert derive.main() == 0
    assert order == ["adopt", "rebuild"]
