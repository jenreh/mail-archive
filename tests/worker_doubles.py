"""The stand-ins two worker test modules share, and nothing that asserts.

A plain module rather than a ``conftest``, the way
``mailarc-analytics/tests/corpus.py`` is: these are values and doubles, not
fixtures, and a ``conftest`` would put them in front of every test in this
directory in order to serve two.

Each double stands in for something that is proven elsewhere against the real
thing — the pipeline in the engine's own tests, the vectors and the analyses in
``mailarc-analytics`` — so that what is tested *here* is the wiring: which kind
of job runs which handler, what reaches the row the UI polls, and what a
handler queues next.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from mailarc_core.archive.model import Message
from mailarc_core.database.entities import MailAccountEntity
from mailarc_core.mail.model import MailProvider, SyncCursorKind
from mailarc_core.mail.ports import MailSourcePort
from mailarc_sync.engine import (
    ImportCounts,
    ImportEngine,
    ImportProgress,
    ImportResult,
    ImportTarget,
)
from mailarc_sync.jobs import JobKind, JobQueue, JobState, SyncJob

ADDRESS = "jens@example.com"
MAILBOX = "/mailboxes/exported"
"""The fake provider's credential is a directory path."""


class FakeSource:
    """A mailbox that only remembers how it was opened and that it was closed."""

    provider = MailProvider.FAKE

    def __init__(self, account: MailAccountEntity, secret: str) -> None:
        self.address = account.email_address
        self.secret = secret
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class GraphNotes:
    """The four :class:`runic.ogm.Session` members the archive writer reaches for.

    Only for the one test that runs the *real* engine end to end: no FalkorDB
    is started here, and the writer's get-before-add needs a ``flush`` that
    really makes an added node findable, or it would look like it works when it
    does not.
    """

    def __init__(self) -> None:
        self.nodes: dict[tuple[type, str], Any] = {}
        self._pending: list[Any] = []

    def get(self, cls: type, pk: str) -> Any:
        return self.nodes.get((cls, pk))

    def add(self, entity: Any) -> None:
        self._pending.append(entity)

    def flush(self) -> None:
        for entity in self._pending:
            self.nodes[(type(entity), entity.id)] = entity
        self._pending.clear()

    def relate(self, source, field, target, edge=None) -> None:
        return None

    def messages(self) -> list[str]:
        """The canonical ids that reached the graph."""
        return sorted(pk for cls, pk in self.nodes if cls is Message)


@contextmanager
def one_graph_session(notes: GraphNotes) -> Iterator[GraphNotes]:
    """What the engine calls when it wants a graph session."""
    yield notes


class RecordingEngine(ImportEngine):
    """Stands in for the pipeline: it says what it was asked to import.

    The pipeline itself runs against real fixtures in the engine's own tests —
    including what a delta does with its cursor. What is only visible from here
    is which *mode* a job kind asks for, so that is what this records.
    """

    def __init__(
        self,
        explode: bool = False,
        archived: int = 2,
        cancelled: bool = False,
    ) -> None:
        self.calls: list[tuple[MailSourcePort, ImportTarget, SyncCursorKind]] = []
        self._explode = explode
        self._new_mail = archived
        self._cancelled = cancelled

    @property
    def modes(self) -> list[SyncCursorKind]:
        """What each run was asked to be."""
        return [mode for _, _, mode in self.calls]

    async def run(
        self,
        source: MailSourcePort,
        target: ImportTarget,
        *,
        mode: SyncCursorKind = SyncCursorKind.FULL,
        on_progress=None,
        cancelled=None,
    ) -> ImportResult:
        self.calls.append((source, target, mode))
        counts = ImportCounts(listed=4, skipped=1, archived=self._new_mail, failed=1)
        if on_progress is not None:
            await on_progress(
                ImportProgress(
                    account_id=target.account_id,
                    counts=counts,
                    estimated_total=9,
                )
            )
        if self._explode:
            raise RuntimeError("the graph went away mid-run")
        now = datetime.now(UTC)
        return ImportResult(
            account_id=target.account_id,
            counts=counts,
            started_at=now,
            finished_at=now,
            mode=mode,
            cancelled=self._cancelled,
        )


class RecordingQueue(JobQueue):
    """The job row, as far as a handler can see it.

    It also stands in for the table *next to* the row: a handler may queue
    follow-up work, and ``already_open`` is how a test says that somebody else
    got there first.
    """

    def __init__(self, already_open: JobKind | None = None) -> None:
        self.reports: list[tuple[int, int, int, int | None]] = []
        self.queued: list[tuple[JobKind, int | None]] = []
        self._already_open = already_open

    async def progress(
        self, job_id: int, done: int, failed: int, total: int | None = None
    ) -> bool:
        self.reports.append((job_id, done, failed, total))
        return True

    async def is_cancel_requested(self, job_id: int) -> bool:
        return False

    async def enqueue(self, kind: JobKind, account_id: int | None = None) -> int:
        self.queued.append((kind, account_id))
        return 100 + len(self.queued)

    async def find_open(
        self, kind: JobKind, account_id: int | None = None
    ) -> SyncJob | None:
        if kind is not self._already_open:
            return None
        return SyncJob(id=99, kind=kind, state=JobState.RUNNING, account_id=account_id)


class CancellingQueue(RecordingQueue):
    """A row whose cancel flag is set from the *n*-th question onwards."""

    def __init__(self, after: int = 0) -> None:
        super().__init__()
        self._after = after
        self.asked = 0

    async def is_cancel_requested(self, job_id: int) -> bool:
        self.asked += 1
        return self.asked > self._after


class RefusingQueue(RecordingQueue):
    """A queue whose table has gone away — a locked file, a vanished directory.

    Only for the follow-up work a handler queues *after* its run is over: what
    the run itself did is already committed by then, so this is the case where
    a second failure must not rewrite the first outcome.
    """

    async def find_open(
        self, kind: JobKind, account_id: int | None = None
    ) -> SyncJob | None:
        raise RuntimeError("database is locked")


def a_job(account_id: int | None, job_id: int = 1) -> SyncJob:
    return SyncJob(
        id=job_id,
        kind=JobKind.IMPORT,
        state=JobState.RUNNING,
        account_id=account_id,
    )


def a_delta_job(account_id: int | None, job_id: int = 1) -> SyncJob:
    """The job a sweep queues — same mailbox, same row, a different question."""
    return SyncJob(
        id=job_id,
        kind=JobKind.INCREMENTAL,
        state=JobState.RUNNING,
        account_id=account_id,
    )


def an_embed_job(job_id: int = 1) -> SyncJob:
    """A job about the whole archive, which is why it names no account."""
    return SyncJob(
        id=job_id, kind=JobKind.EMBED, state=JobState.RUNNING, account_id=None
    )


def a_derive_job(job_id: int = 1) -> SyncJob:
    """A job about the whole archive, which is why it names no account."""
    return SyncJob(
        id=job_id, kind=JobKind.DERIVE, state=JobState.RUNNING, account_id=None
    )
