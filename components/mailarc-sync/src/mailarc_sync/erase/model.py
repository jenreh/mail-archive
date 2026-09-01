"""What one clear-out did, and the one thing that stops it happening.

No I/O, and nothing imported from below it.
"""

from pydantic import BaseModel, ConfigDict

from mailarc_core.archive import PurgeCounts


class EraseCounts(BaseModel):
    """The whole result of clearing one mailbox, store by store.

    Reported rather than summed into a single number because the four counts
    mean four different things, and a mismatch between them is the interesting
    signal: ``messages`` far below ``archived_rows`` says the graph had already
    lost what the ledger still claimed, and ``copies`` above zero says some of
    this mail is still in the archive under another mailbox.
    """

    model_config = ConfigDict(frozen=True)

    messages: int = 0
    """``Message`` nodes deleted — mail no other account also holds."""

    copies: int = 0
    """Provenance edges dropped off mail another account holds as well.

    Those messages stay in the archive. They are simply no longer attributed to
    the mailbox that was cleared.
    """

    archived_rows: int = 0
    """Rows removed from the archived-message ledger — what the import skips by."""

    checkpoints: int = 0
    """Sync checkpoints forgotten — what the import would otherwise resume from."""

    failures: int = 0
    """Rows removed from the ledger of messages this mailbox could not import."""

    @classmethod
    def of(
        cls,
        purged: PurgeCounts,
        *,
        archived_rows: int,
        checkpoints: int,
        failures: int,
    ) -> EraseCounts:
        """Fold the graph half and the relational half into one answer."""
        return cls(
            messages=purged.messages,
            copies=purged.copies,
            archived_rows=archived_rows,
            checkpoints=checkpoints,
            failures=failures,
        )


class AccountBusy(Exception):
    """Raised instead of clearing a mailbox a job is still working on.

    Its own type rather than a ``ValueError``, because the caller acts on it:
    this is the one failure a human fixes by waiting, and the page says so
    rather than showing it beside the errors that mean something is broken.
    """
