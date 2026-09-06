"""What the repair panel shows, before any of it is a state var.

Frozen value objects and no I/O, the rule every ``model`` in this package
keeps. The sentences live here too: what a panel says about an archive is a
decision, and a decision spelled inline in a component is one nobody can test
without rendering it.
"""

from pydantic import BaseModel, ConfigDict

from mailarc_sync.jobs import JobState, SyncJob

REPAIR_ICON = "unlink"
"""What the card wears: this is about messages that are not joined up."""

TITLE = "Conversations"

EXPLANATION = (
    "A message is grouped with its replies through the conversation its "
    "headers name. Mail archived before the conversation key learned to fall "
    "back to a message's own Message-ID carries no conversation at all, so "
    "the first message of an old exchange is listed beside its own replies "
    "instead of under them. Repairing files those messages; nothing is "
    "fetched, changed or deleted."
)
"""Why the button exists, said once, where a person meets it.

Long, and deliberately so: this is the administration, the number beside it is
alarming the first time it is read, and "735 messages sit in no conversation"
without the reason reads as data loss rather than as a missing edge.
"""

WHOLE = "Every message sits in a conversation."
"""What an archive that needs nothing says. Not an empty panel: "nothing to
repair" is a fact worth stating on a page somebody opened to check."""

BUSY = "The archive is being repaired. This page follows it."
NOT_READ = "The archive has not been asked yet."

_STATE_COLORS = {
    JobState.QUEUED: "gray",
    JobState.RUNNING: "blue",
    JobState.SUCCEEDED: "green",
    JobState.FAILED: "red",
    JobState.CANCELLED: "orange",
}
"""One colour per end a job can come to, and one for the two that are not ends."""


class RepairJobView(BaseModel):
    """The repair job as the control shows it.

    No percentage and no bar, unlike the rebuild's view: a repair writes its
    tally into the row **once, at the end**, because the numbers a person is
    waiting for — how many messages were filed — are only known when the walk
    is over. So ``done`` out of ``total`` here is a result and not a position,
    and it is shown as a sentence rather than as a bar that would sit at zero
    and then jump to full.
    """

    model_config = ConfigDict(frozen=True)

    job_id: int = 0
    status: str = ""
    status_color: str = "gray"
    error: str = ""
    active: bool = False
    filed: int = 0
    scanned: int = 0
    unkeyed: int = 0
    reported: bool = False
    """Whether the row carries a tally yet. False for a job that is still
    walking, and for one that failed before it wrote anything."""

    @classmethod
    def from_job(cls, job: SyncJob) -> RepairJobView:
        return cls(
            job_id=job.id,
            status=str(job.state),
            status_color=_STATE_COLORS.get(job.state, "gray"),
            error=job.error or "",
            active=job.state in (JobState.QUEUED, JobState.RUNNING),
            filed=job.progress.done,
            scanned=job.progress.total,
            unkeyed=job.progress.failed,
            reported=job.progress.total > 0,
        )


NO_JOB = RepairJobView()
"""Nothing queued yet. A sentinel keeps the component free of ``None``."""


def outside_label(count: int) -> str:
    """How many messages are in no conversation, as the panel says it.

    Singular where it is one message, because the number this reports is
    usually large once and then zero — and the one time it is ``1`` is exactly
    when a reader is checking whether the repair worked.
    """
    if count <= 0:
        return WHOLE
    if count == 1:
        return "1 message sits in no conversation."
    return f"{count:,} messages sit in no conversation."


def repaired_label(filed: int, scanned: int, unkeyed: int) -> str:
    """What a finished run says it did, in the numbers that mean something.

    ``filed`` is what a reader sees change in the result list, and ``scanned``
    is what it was found among. ``unkeyed`` is only mentioned when it is not
    zero — a message with no ``Message-ID`` is a fact about that message and
    not a failure of the run, so printing "0 could not be keyed" every time
    would make it look like one.
    """
    said = f"Filed {filed:,} of {scanned:,} messages into their conversations."
    if unkeyed:
        said += f" {unkeyed:,} carry no Message-ID and were left alone."
    return said
