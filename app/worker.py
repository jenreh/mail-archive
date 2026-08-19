"""The worker process: what a job kind means, and who can carry it out.

Here rather than in ``mailarc-sync`` because this is the layer that is allowed
to name implementations (§4.1). The engine walks a mailbox through a port and
must not be able to say "Gmail"; the loop in ``mailarc_sync.jobs.worker`` runs
whatever it is handed. So the wiring — which kind of job runs which handler,
where the credential comes from, which store the bytes land in — lives in the
composition root, and this module is the composition root of its own process.

Started by :func:`app.composition.sync_worker_lifespan` on the desktop, or as
its own unit under Docker and systemd, and always as ``python -m app.worker``.
Deliberately lean: it reaches ``app.configuration`` and ``app.composition``,
and neither of those pulls Reflex in. ``tests/test_worker.py`` proves that from
outside instead of trusting it.
"""

import asyncio
import logging
from functools import partial

from appkit_commons.database.session import get_asyncdb_session
from sqlalchemy.ext.asyncio import AsyncSession

from app.composition import (
    archive_config,
    graph_config,
    mail_config,
    provider_registry,
    sync_config,
)
from app.configuration import configure
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.database.entities import CredentialKind
from mailarc_core.database.repositories import (
    MailAccountRepository,
    MailCredentialRepository,
)
from mailarc_core.graph.client import session as graph_session
from mailarc_core.mail.errors import MailAuthError
from mailarc_core.mail.model import MailProvider
from mailarc_core.mail.ports import MailSourcePort
from mailarc_sync.engine import (
    ImportEngine,
    ImportProgress,
    ImportTarget,
    ProviderRegistry,
)
from mailarc_sync.jobs import (
    JobHandler,
    JobKind,
    JobQueue,
    JobWorker,
    SessionFactory,
    SyncJob,
)

logger = logging.getLogger(__name__)

_ACCOUNTS = MailAccountRepository()
_CREDENTIALS = MailCredentialRepository()


def build_engine() -> ImportEngine:
    """The import pipeline, wired to this installation's stores."""
    archive = archive_config()
    return ImportEngine(
        config=sync_config(),
        blobs=BlobStore(archive),
        archiver=MessageArchiver(archive),
        graph_session=partial(graph_session, graph_config()),
        database_session=get_asyncdb_session,
        mail_config=mail_config(),
    )


def build_handlers(
    engine: ImportEngine,
    registry: ProviderRegistry,
    session_factory: SessionFactory = get_asyncdb_session,
) -> dict[JobKind, JobHandler]:
    """Which kind of job runs what.

    One entry, because one kind can be carried out today: ``incremental``
    arrives with phase 7, ``derive`` with phase 5, ``embed`` with phase 6.
    Until then a job of those kinds fails with "no handler registered", which
    is both what the loop does with an unmapped kind and the truth.
    """
    return {JobKind.IMPORT: partial(_import, engine, registry, session_factory)}


async def run_worker() -> None:
    """Build everything a worker needs and run its loop until it stops."""
    config = sync_config()
    queue = JobQueue()
    worker = JobWorker(
        queue,
        build_handlers(build_engine(), provider_registry()),
        worker_id=config.worker_id,
        lease_seconds=config.lease_seconds,
        heartbeat_seconds=config.heartbeat_interval,
        poll_seconds=config.poll_interval,
    )
    await worker.run()


def main() -> None:
    """``python -m app.worker``: configure this process, then run the loop."""
    logging.basicConfig(level=logging.INFO)
    # Importing `app` already configured the process; saying so here keeps the
    # worker's dependency on configuration a statement rather than a side
    # effect. `configure` is cached, so this is free.
    configure()
    asyncio.run(run_worker())


async def _import(
    engine: ImportEngine,
    registry: ProviderRegistry,
    session_factory: SessionFactory,
    job: SyncJob,
    queue: JobQueue,
) -> None:
    """Walk one account's mailbox, reporting into the row the UI reads.

    Errors are not caught here on purpose: the taxonomy of §7.6 is the loop's
    to act on, and swallowing an auth failure would cost a mailbox its
    re-consent prompt.
    """
    async with session_factory() as session:
        source, target = await _open_mailbox(registry, session, job)
    try:
        await engine.run(
            source,
            target,
            on_progress=partial(_report, queue, job.id),
            cancelled=partial(queue.is_cancel_requested, job.id),
        )
    finally:
        await source.aclose()


async def _open_mailbox(
    registry: ProviderRegistry, session: AsyncSession, job: SyncJob
) -> tuple[MailSourcePort, ImportTarget]:
    """The source and the target for one job, built while the row is live.

    Both inside the caller's session on purpose: a factory reads what it needs
    off the account row, and a row whose session has closed hands back nothing.
    """
    if job.account_id is None:
        raise LookupError(f"job {job.id} imports, but names no account")
    account = await _ACCOUNTS.find_by_id(session, job.account_id)
    if account is None:
        raise LookupError(f"job {job.id} names account {job.account_id}, which is gone")

    provider = MailProvider(account.provider)
    source = registry.factory_for(provider)(
        account, await _secret_for(session, account.id)
    )
    target = ImportTarget(
        account_id=account.id, address=account.email_address, provider=provider
    )
    logger.info("Job %d imports %s (%s)", job.id, account.email_address, provider)
    return source, target


async def _secret_for(session: AsyncSession, account_id: int) -> str:
    """The account's stored secret, whichever kind it turns out to be.

    Which kind an account has is the provider's business — OAuth for Gmail, a
    password for IMAP — and there is at most one per kind, so the first one
    found is the one that opens the mailbox. Having none is not a mailbox
    fault but an unfinished setup, and :class:`MailAuthError` is what puts that
    in front of a human instead of retrying it.
    """
    for kind in CredentialKind:
        credential = await _CREDENTIALS.find_by_account(session, account_id, kind)
        if credential is not None:
            return credential.secret
    raise MailAuthError(f"account {account_id} has no stored credential")


async def _report(queue: JobQueue, job_id: int, progress: ImportProgress) -> None:
    """Copy a page's tally into the job row.

    A message that was already ours counts as done rather than as nothing: the
    number a progress bar needs is how much of the mailbox has been dealt with.
    """
    counts = progress.counts
    await queue.progress(
        job_id,
        done=counts.archived + counts.skipped,
        failed=counts.failed,
        total=progress.estimated_total,
    )


if __name__ == "__main__":
    main()
