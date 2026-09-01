"""What a run is about, what it carries between its stages, and what it did.

No I/O, no provider vocabulary — the engine's own values, all frozen, so a
progress snapshot handed to a UI cannot be edited behind the run's back and a
result is a record rather than a live view.

:class:`PreparedMessage` and :class:`MessageFailure` are the two things that
travel the queue between the fetch stage and the single archive consumer. They
are a pair on purpose: a message that could not be parsed keeps travelling as a
value, so the consumer that writes the archive is also the one that writes the
row saying why a message is missing from it. There is no third outcome, and
therefore no way to drop a message silently.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from mailarc_core.archive.model import ArchiveSource
from mailarc_core.mail.model import MailProvider, ParsedMessage, SyncCursorKind


class ImportTarget(BaseModel):
    """The mailbox one run walks, reduced to what the pipeline actually needs.

    Not the account row: the engine hands these three values to worker threads
    and puts them on graph nodes, and a detached ORM instance is the wrong
    thing to do either with. The composition root reads the row once and builds
    this.
    """

    model_config = ConfigDict(frozen=True)

    account_id: int
    """The ``mail_accounts`` row id — the key every relational table joins on."""

    address: str
    provider: MailProvider


class ImportCounts(BaseModel):
    """The tally of one batch, and the shape a whole run adds up to.

    Four numbers that have to stay separable: a message the provider offered
    (``listed``) either was already ours (``skipped``), reached the graph
    (``archived``), or left a row saying why it did not (``failed``). Collapsing
    the last two would make a broken mailbox look like a small one.
    """

    model_config = ConfigDict(frozen=True)

    listed: int = 0
    skipped: int = 0
    archived: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        """Everything dealt with — what the checkpoint interval counts."""
        return self.skipped + self.archived + self.failed

    def plus(self, other: ImportCounts) -> ImportCounts:
        """Fold another batch's tally into this one."""
        return ImportCounts(
            listed=self.listed + other.listed,
            skipped=self.skipped + other.skipped,
            archived=self.archived + other.archived,
            failed=self.failed + other.failed,
        )


class ImportProgress(BaseModel):
    """Where a run stands, as often as the caller cares to be told.

    Reported once per page, which is also the only point at which the numbers
    are consistent: mid-page the fetch stage is ahead of the archive stage.
    """

    model_config = ConfigDict(frozen=True)

    account_id: int
    counts: ImportCounts
    estimated_total: int | None = None
    """The provider's own guess at the mailbox size. Allowed to be wrong."""


class ImportResult(BaseModel):
    """What one run did, once it has stopped doing it."""

    model_config = ConfigDict(frozen=True)

    account_id: int
    counts: ImportCounts
    started_at: datetime
    finished_at: datetime
    cursor: str | None = None
    """The token the next run resumes from; ``None`` when the mailbox ran out."""

    cancelled: bool = False
    """True when the caller asked to stop between two batches, not an error."""

    mode: SyncCursorKind = SyncCursorKind.FULL
    """What the run *ended* as, which is not always what it was asked for.

    A delta whose cursor the provider rejected finishes as a full walk, and the
    caller has to be able to see that without reading a log — a delta that
    reports thousands of messages is otherwise indistinguishable from a bug.
    """

    resynced: bool = False
    """True when an expired cursor turned this run into a full walk."""


class PreparedMessage(BaseModel):
    """A message that is parsed, stored on disk, and ready for the graph.

    Everything expensive has happened by the time one of these exists: the
    bytes are fetched, parsed and in the blob store. All the archive consumer
    still has to do is write nodes and edges, which is why it can afford to be
    the only one doing it.
    """

    model_config = ConfigDict(frozen=True)

    source: ArchiveSource
    message: ParsedMessage


class MessageFailure(BaseModel):
    """One message the import gave up on — the row, before it is a row.

    Skipping is allowed, silence is not. This value is what carries a skipped
    message to the stage that owns the database session, so *not* writing it
    down would take deleting code rather than forgetting to add some.
    """

    model_config = ConfigDict(frozen=True)

    provider_message_id: str
    reason: str
    """The error taxonomy's short name, as ``mail_failed_messages`` stores it."""

    detail: str | None = None
