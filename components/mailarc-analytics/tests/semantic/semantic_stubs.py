"""Stand-ins for the two things a semantic test must not use for real.

An embedder is an HTTP call to somebody else's process, and a graph session is
a server. Neither belongs in a test of what happens *around* them — whether the
right error is raised with no embedder configured, whether a page is written
once, whether a cancel is honoured between pages.

A module of its own rather than a ``conftest``: a test module cannot reliably
``import conftest`` — the name is pytest's convention rather than a package
path, so in a repository-wide run the first one imported claims it. The
component's own ``planted_graph.py`` exists for the same reason and says so.

The name is deliberately not ``stubs``: every component in this workspace puts
its test helpers on the same ``sys.path``, and two modules called ``stubs``
would resolve to whichever was imported first.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, cast

from runic.ogm import Session, Vector

from mailarc_analytics.semantic.ports import EmbedPurpose
from mailarc_core.archive.reader import GraphSessionFactory


class StubEmbedder:
    """An embedder that answers instantly and remembers what it was asked.

    The vectors are a function of the text's length, which is enough to make
    two different texts land in different places and the same text land in the
    same one — everything a search or a clustering test needs, without a model.

    ``misbehave_from`` says at which call the configured failure starts. It
    exists because the job probes the embedder once before the loop, so an
    embedder that failed from the first call would only ever test the guard —
    ``misbehave_from=2`` lets the probe succeed and puts the failure inside the
    loop, where the per-batch handling lives.
    """

    def __init__(
        self,
        *,
        model: str = "stub-model",
        dimension: int = 4,
        error: Exception | None = None,
        truncate: int = 0,
        misbehave_from: int = 1,
    ) -> None:
        self.model = model
        self.dimension = dimension
        self.calls: list[tuple[tuple[str, ...], EmbedPurpose]] = []
        self.closed = 0
        self._error = error
        self._truncate = truncate
        self._misbehave_from = misbehave_from

    async def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbedPurpose = EmbedPurpose.DOCUMENT,
    ) -> Sequence[Vector]:
        self.calls.append((tuple(texts), purpose))
        misbehaving = len(self.calls) >= self._misbehave_from
        if self._error is not None and misbehaving:
            raise self._error
        vectors = [self.vector_for(one) for one in texts]
        if self._truncate and misbehaving:
            return [Vector(one[: self._truncate]) for one in vectors]
        return vectors

    async def aclose(self) -> None:
        self.closed += 1

    def vector_for(self, text: str) -> Vector:
        """The vector this stub will answer for *text* — deterministic.

        Exposed so a test can plant a message's vector in a graph and then ask
        for the same text back, which is what makes a KNN assertion about
        *ranking* rather than about floating point.
        """
        values = [0.0] * self.dimension
        values[0] = 1.0
        values[1] = min(len(text), 90) / 100
        return Vector(values)

    @property
    def texts(self) -> list[str]:
        """Every text this embedder was handed, flattened across calls."""
        return [one for call, _ in self.calls for one in call]


def once(rows: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    """One answer, given to every call of a statement."""
    return [[dict(one) for one in rows]]


def then(*answers: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    """A sequence of answers, one per call, the last one repeating.

    How a paged read is written down: the loop stops on an empty page, so a
    session that answered the same page forever would never terminate — which
    is exactly the bug this shape catches if the cursor is ever dropped.
    """
    return [[dict(row) for row in one] for one in answers]


class RecordingSession:
    """A runic session that answers from a script instead of from a store.

    Keyed by the statement itself, because the statements are module-level
    constants in the catalogue and a test that keyed on a name would still
    pass if the caller ran a different one.

    ``errors`` makes one statement raise instead of answering, which is how the
    driver's own refusals are written down — a real one is a
    ``redis.exceptions.ResponseError``, and this package may not import
    ``redis`` to build one.
    """

    def __init__(
        self,
        answers: Mapping[str, Sequence[Sequence[Mapping[str, Any]]]],
        errors: Mapping[str, Exception] | None = None,
    ) -> None:
        self._answers = {name: list(queue) for name, queue in answers.items()}
        self._errors = dict(errors or {})
        self._calls: dict[str, int] = {}
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: Mapping[str, Any]) -> Any:
        self.executed.append((statement, dict(params)))
        failure = self._errors.get(statement)
        if failure is not None:
            raise failure
        queue = self._answers.get(statement, [[]])
        index = min(self._calls.get(statement, 0), len(queue) - 1)
        self._calls[statement] = self._calls.get(statement, 0) + 1
        return _Result(queue[index])

    def statements(self) -> list[str]:
        return [statement for statement, _ in self.executed]

    def params_for(self, statement: str) -> list[dict[str, Any]]:
        return [args for one, args in self.executed if one == statement]


class _Result:
    """What ``Session.execute`` hands back: a header and a list of lists."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.columns = tuple(rows[0]) if rows else ()
        self.rows = [[row[column] for column in self.columns] for row in rows]


def as_session(recording: RecordingSession) -> Session:
    """The recording session, typed as what the functions under test expect.

    The same shape ``tests/queries/test_queries_rows.py`` uses: a stub answers
    ``execute`` and nothing else, and a cast at the seam is cheaper than
    faking a whole runic ``Session``.
    """
    return cast(Session, recording)


def sessions_from(session: RecordingSession) -> GraphSessionFactory:
    """A session factory handing out the same recording session every time."""

    @contextmanager
    def factory() -> Iterator[Session]:
        yield as_session(session)

    return factory


def no_sessions() -> GraphSessionFactory:
    """A session factory that fails if anything opens a session.

    The point of several tests below: with no embedder configured, a semantic
    search must raise *before* it touches the graph. A factory that merely
    returned nothing would let a caller pass by doing the wrong thing quietly.
    """

    @contextmanager
    def factory() -> Iterator[Session]:
        raise AssertionError("the graph was opened when nothing should have been read")
        yield  # pragma: no cover - unreachable, keeps this a generator

    return factory
