"""Repairing the archive's conversations: once, now, against the configured graph.

Here rather than in ``mailarc-core`` for the reason :mod:`app.derive` is here:
:func:`~mailarc_core.archive.threads.repair_threads` knows how to file a
message into the conversation its headers name, it does not know *which* graph
this installation keeps. A component that opened its own session would have to
read configuration, and §4.1 leaves that to the composition root.

What it fixes is one missing rule, and one that could not be applied
retroactively by any import. The writer keys a conversation on the provider's
thread id, else on the root of ``References``, else — since the fallback landed
— on the message's own ``Message-ID``, which is what gives a conversation's
first message a node at all. Mail archived before that carries no ``IN_THREAD``
edge, and the import engine never rewrites mail the ledger already knows, so
the root of every old exchange sits in the result list beside its own replies
instead of under them.

Two callers, one function, exactly as the rebuild has: ``python -m app.repair``
runs it and waits, and the ``repair`` job in :mod:`app.worker` calls
:func:`repair` inside ``asyncio.to_thread`` and reports into the row the
administration page follows. Both get the same repair, because there is only
one, and a second way to start one would eventually be a second way to
configure it.

The command prints nothing. What a repair did belongs in the log beside what
the worker logs about the same work, not on a stdout only one of the two
callers has.
"""

import logging

from app.composition import graph_config
from app.configuration import configure
from mailarc_core.archive import ThreadRepairCounts, repair_threads
from mailarc_core.archive.threads import ProgressHook
from mailarc_core.graph.client import session as graph_session

logger = logging.getLogger(__name__)


def repair(on_progress: ProgressHook | None = None) -> ThreadRepairCounts:
    """File every message that sits in no conversation. Returns what it did.

    Blocking, because every runic driver is: an async caller wraps this in
    ``asyncio.to_thread``, and ``on_progress`` is then called from that thread
    once per page.

    The session is opened and closed around the one repair rather than kept: a
    repair is a command, not a service, and a driver held open between two of
    them would be a connection nobody is using while the archive is idle.
    """
    with graph_session(graph_config()) as session:
        counts = repair_threads(session, on_progress=on_progress)
    logger.info(
        "Conversations repaired over %d messages: %d messages filed, "
        "%d conversations opened, %d messages could not be keyed",
        counts.scanned,
        counts.linked,
        counts.threads,
        counts.unkeyed,
    )
    return counts


def main() -> int:
    """``python -m app.repair``: repair the conversations of this archive.

    A failure is logged with its traceback and answered with a non-zero exit
    rather than a raised exception, because the caller is a shell task: what a
    task can act on is the status, and a stack trace on stderr is for the
    person reading afterwards.
    """
    logging.basicConfig(level=logging.INFO)
    # Importing `app` already configured the process; saying so here keeps this
    # command's dependency on configuration a statement rather than a side
    # effect. `configure` is cached, so this is free.
    configure()
    try:
        repair()
    except Exception:
        logger.exception("The conversations could not be repaired")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
