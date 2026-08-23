# Jobs and the worker

An import runs for hours and a laptop lid closes. So progress is a **row**, not a
stack frame: a job is claimed under a lease, reports what it got done, and — if
the worker dies — is taken over by the next one at the checkpoint the dead one
wrote.

![Job lifecycle](../diagrams/job-lifecycle.svg)

## Why not the appkit scheduler

It was measured against this and does not fit. It is a cron with a trigger, not
a work queue with a lease, and it silently falls back to an in-memory scheduler
on anything but PostgreSQL. Hence a table of our own.

`JobQueue` is deliberately **not** a scheduler either. There is no trigger, no
cron and no calendar in it. Something else decides *when* a job should exist;
this decides who gets to run it and what happened. That something else is
[`IntervalScheduler`](#the-recurring-trigger), an interval loop in its own
module beside the queue — it enqueues and returns, and everything after that is
the ordinary path a job queued by a button takes.

## The state machine

```text
queued ──claim──> running ──> succeeded
                     │└──────> failed      (with error)
                     └───────> cancelled   (cancel_requested)
```

Plus one edge back: a lease that stops moving returns `running` to `queued`.

## The claim is a compare-and-swap

```python
UPDATE mail_sync_jobs
   SET state='running', worker_id=?, lease_until=?, heartbeat_at=?, ...
 WHERE id=? AND state='queued'
```

`rowcount` decides who won. SQLite has no `SKIP LOCKED` and does not need one —
a compare-and-swap lets exactly one worker through, and the loser tries the next
candidate.

Every method opens its own session and commits it. That is the point: a lease
that exists only inside our transaction protects nothing from the other process.

Two details that look small and are not:

- **`_queued_ids` selects ids, never entities.** Loading a job there would put
  it in the session's identity map, and the row read back after the swap has to
  be the row the database has, not the one we saw before it.
- **`started_at` is stamped with `COALESCE`**, so a job resumed after a crash
  still reports when its work actually began.

## The lease is the whole ownership story

While `lease_until` is in the future, the job is that worker's. When it stops
moving, the job is up for grabs.

| Setting | Default | Role |
| --- | --- | --- |
| `lease_seconds` | 120 | How long a claim survives without a sign of life |
| `heartbeat_interval` | 10 | How often the lease is pushed out |

Generously apart, on purpose: a worker that is merely slow must not have its job
stolen while it still holds the graph session.

### The crash story is one timestamp

```python
await queue.reclaim_expired()
```

A worker that dies stops heartbeating, its lease expires, and the next worker
takes the job over and resumes from the checkpoint. No lock server, no liveness
protocol — a timestamp that stopped moving says everything.

`worker_id` is `<pid>@<hostname>` because that is what a human needs when a job
has been `running` for an hour: what to kill and where. Pid first, so a very long
hostname loses the half that does *not* tell two workers apart.

### Every write is guarded on `running`

Including `progress()`. A worker whose lease expired mid-page is still holding
counters, and without the guard its last report would land on top of the numbers
the worker that took over is now producing — a progress bar that walks backwards.
`False` says the job had already moved on.

The exception is `_finish`, which is unconditional: the caller holds the lease,
so it is the one entitled to say how the job ended.

## Cancellation is a flag, not a kill

`request_cancel` sets `cancel_requested`. The handler reads it **between
batches**, so whatever was half-written when a human clicked cancel is still
written whole.

And the flag, not the handler, decides the outcome. A handler stops by returning
between batches; the queue is the only place that knows whether that return was
the end of the work or the answer to a cancel:

```python
if await self._queue.is_cancel_requested(job.id):
    await self._queue.cancel(job.id)
else:
    await self._queue.succeed(job.id)
```

## The worker loop

[`mailarc_sync.jobs.worker.JobWorker`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-sync/src/mailarc_sync/jobs/worker.py)
holds the poll loop and nothing else. No process management, no configuration
building, no knowledge of what an `import` job actually does — `app/worker.py`
owns all three, because that is the composition root and this is a library.

```text
sweep expired leases → claim → run → repeat
```

The sweep comes first, so a dead worker's job comes back before this one asks for
new work.

One job at a time, one worker per process. Serialising the archive writer is
cheaper than coordinating several, and a queue that hands out one job at a time
needs no fairness rules.

### Three tasks race while a job runs

```python
done, _ = await asyncio.wait({work, beat, stop}, return_when=FIRST_COMPLETED)
```

| Winner | Meaning |
| --- | --- |
| `work` | The handler finished. Await it for its outcome |
| `beat` | **The job is not ours any more.** Let go of it |
| `stop` | We were asked to shut down. The job keeps its lease until it expires |

The heartbeat is waited on *alongside* the work for exactly that middle case. A
handler that kept running past losing its lease would import a mailbox a second
worker is already importing, and then write an outcome onto that worker's job.

Note what the heartbeat task returning means: it returns only on a **refusal**
from the queue. A database hiccup while extending the lease is logged and retried
on the next beat — the lease is a safety net, not the work, and an error there
must not replace the outcome the handler has earned.

### Shutdown takes the same path as `kill -9`

A stop request **abandons** the job rather than ending it. The lease runs out,
`reclaim_expired` puts it back, and the next start resumes at the checkpoint.

That is deliberately the same path a `kill -9` takes. One path is worth more than
two, because it is the one that gets exercised.

`SIGTERM` and `SIGINT` are wired to the stop request, and unwired on the way out.
A signal must not end the process mid-write. Installing a handler fails outside
the main thread and on platforms without one — and a worker that cannot hear
`SIGTERM` is still a worker, so that failure is logged at debug and shrugged off.

### The handler contract

```python
type JobHandler = Callable[[SyncJob, JobQueue], Awaitable[None]]
```

A handler gets the job and the queue, and that is the whole contract: the queue
is how it reports progress and how it asks, between batches, whether it should
stop. Anything else it needs comes from a closure built in `app/worker.py`. A
context object here would only move the wiring one layer down.

### Retries: one, on purpose

```python
DEFAULT_MAX_ATTEMPTS = 1
```

The handler is the layer that knows what to retry. The import engine already
retries a *slice* five times with backoff before letting a `MailTransientError`
out, so a second budget here would multiply into twenty-five attempts and four
extra walks of the whole mailbox — for an error that by then means an outage, not
a hiccup.

Raise it for a handler that does no retrying of its own; the loop still knows how
to back off.

### What each failure does to the job

| Raised | Job | Side effect |
| --- | --- | --- |
| `MailAuthError` | `failed` | Account → `auth_error`, `last_error` set. The UI offers re-consent |
| `MailTransientError` | `failed` after the attempt budget | Backoff between attempts |
| `MailPermanentError` | `failed` | A row in `mail_failed_messages` — even though we no longer know which message it was, because a drop nobody can count is the one thing forbidden |
| anything else | `failed` | `TypeName: message` in `error`, full traceback logged |

## The composition root of the worker process

[`app/worker.py`](https://github.com/jenreh/mail-archive/blob/main/app/worker.py) is where a job kind becomes a real piece
of work:

```python
def build_handlers(engine, registry, session_factory, rebuild, embed):
    return {
        JobKind.IMPORT: partial(_import, ..., mode=SyncCursorKind.FULL),
        JobKind.INCREMENTAL: partial(_import, ..., mode=SyncCursorKind.INCREMENTAL),
        JobKind.DERIVE: partial(_derive, rebuild),
        JobKind.EMBED: partial(_embed, embed),
    }
```

One entry per `JobKind`, and two of them are the same function. `import` walks a
whole mailbox and `incremental` asks the same engine what changed since the last
run; everything a *job* adds to a run — finding the account, decrypting its
credential, storing the one the provider rotated mid-flight, reporting into the
row the UI polls — is identical, so only the mode is bound differently. A second
handler would be the same twenty lines with one argument changed, and two places
to forget the credential in. `derive` recomputes what the whole archive means,
and `embed` fills in the vectors the import never writes.

A kind with no handler fails with "no handler registered", which is what the
loop does with an unmapped kind. `tests/test_worker.py` asserts the mapping
against `JobKind` itself, so the next kind that arrives without a handler fails
a test rather than a user's job.

`_open_mailbox` builds the source **inside the caller's session**: a factory
reads what it needs off the account row, and a row whose session has closed hands
back nothing.

A `derive` job names no account — `account_id` is `None`, because the rebuild is
about the whole archive — and nothing on that path asks for one. It runs
[`app.derive.rebuild`](https://github.com/jenreh/mail-archive/blob/main/app/derive.py)
inside `asyncio.to_thread`, because every runic driver blocks and a full read of
the archive would otherwise hold up every page watching the job. The row moves
in **stages, not messages**: a rebuild reads the whole archive at each of its
five stages, so `done` counts the stages behind it and `total` is five. The
cancel flag is read at each of those stage boundaries, from the rebuild's own
thread, and a rebuild abandoned there costs nothing — the next one deletes what
it left before writing anything.

Errors are not caught in the handler on purpose. The taxonomy is the loop's to
act on, and swallowing an auth failure would cost a mailbox its re-consent
prompt.

## The recurring trigger

Nothing syncs on its own until it is turned on:

```yaml
app:
  sync:
    incremental_interval: 900   # seconds; 0 (the default) is off
```

Off by default because a fresh install must not start talking to somebody's
mailbox on its own — the first sync is a button a human presses.

[`IntervalScheduler`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-sync/src/mailarc_sync/jobs/scheduler.py)
wakes every interval and queues one `incremental` job per mailbox that owes
one. It **enqueues and nothing else**: it reads `mail_accounts`, inserts into
`mail_sync_jobs`, and never opens a mailbox — whether a provider can answer
"what changed since?" is read off its `ProviderDescriptor.supports_incremental`,
which is what keeps the engine unable to name Gmail.

A sweep skips an account that

- is disabled — `enabled` is the human's switch, and a schedule is not a human;
- is in `auth_error` — nothing but a re-consent moves it out, so queueing one
  would fail a job, hammer the provider and fill the table with identical
  failures every interval;
- has an open `incremental` **or** `import` job. Both kinds write the same
  full-scope checkpoint and both insert into `mail_archived_messages`, whose
  unique key turns the second writer's batch into an `IntegrityError` — a sweep
  must not break the import a human just started;
- names a provider that has no delta, or one this build never registered;
- **has never finished a full import.** A delta over a mailbox nobody has walked
  has no history to ask about: the engine bootstraps at today's watermark,
  archives nothing and reports success, and every sweep after that fetches only
  what arrived since — leaving whatever was already in the mailbox in no run at
  all, with nothing anywhere saying so. The signal is the `incremental`-scope
  row in `mail_sync_checkpoints`, which is exactly what a finished, uncancelled
  run leaves behind. Not the full-scope row: an import cancelled halfway leaves
  one of those, and a mailbox that was imported and then re-imported halfway
  would stop getting deltas it has every right to.

The wait comes **before** the first sweep. A desktop application restarts every
time a lid closes, and sweeping on startup would turn each of those into a round
of syncs.

It cannot take the worker down: a sweep that throws is logged and the loop goes
back to waiting, and an account that throws is logged and the next one is tried.
`app/worker.py` starts it beside the poll loop and puts it down in a `finally`,
whatever ended that loop — `worker.run()` stays the thing that ends the process,
which an `asyncio.TaskGroup` would not allow, since it waits for every task it
holds.

### Why not appkit's `APScheduler`

It is the obvious alternative and it was measured, with three findings each
sufficient on its own:

- **Not installed.** `from appkit_commons.scheduler import APScheduler` yields
  literally `None` in this environment, because that import sits inside a
  `try/except ImportError`.
- **PostgreSQL-bound.** `_configure_scheduler` builds a `PsycopgEventBroker`
  from the database url; on `sqlite+aiosqlite://` the constructor throws into an
  `except Exception` that falls back to an in-memory `AsyncScheduler`
  **silently** — persistence and cross-process coordination gone with nothing
  turning red.
- **A cron, not a work queue.** Its `Scheduler` ABC knows
  `add_service(ScheduledService)` with a trigger, and no single enqueue, no
  per-job progress, no cancel, no lease — the four things a mailbox import needs
  most.

It becomes worth wiring up for a PostgreSQL deployment, where the persistence it
wants is really there. On a desktop it would be a dependency that quietly does
nothing.

### After a delta: a rebuild, not an incremental recomputation

An `incremental` job that archived at least one message queues a `derive` job;
one that archived nothing, or that failed, queues nothing. Nor does one that was
asked to stop — and the *row's* cancel flag is read as well as the run's own
view of itself, because a delta is usually a single page and the page loop
breaks on "no next cursor" before it asks whether to stop. Reading only the run
would answer a human's stop with an hour of graph writes.

Queueing the rebuild happens after the handler's `finally` and inside a
`try/except` of its own. After it, so a run that raised queues nothing — half a
mailbox in the graph is not worth recomputing over. Inside its own guard,
because by then the mail is archived and the watermark has moved: a database
that goes away while the follow-up is enqueued must not turn a finished sync
into a row saying it failed.

That `derive` is the **ordinary full rebuild** and deliberately not an
incremental recomputation of the derived layer. The rebuild deletes that layer
before writing it, which makes running it again idempotent and running it often
merely expensive — while a genuinely incremental derive is a much larger piece
of work, because none of the three analyses is local to the messages that just
arrived: a co-recipient group, a topic and a template are each a statement about
the whole archive, and one new mail can move any of them.

Only after a delta, because a full import is something a human started and is
watching, on a page that offers the rebuild button next to it. And only while no
rebuild is already open, for the reason `find_open` exists: two rebuilds
interleaving delete each other's rows.

## Running it

```sh
task sync:worker           # foreground, its own process
task graph:rebuild-derived # one rebuild, no queue, no worker
```

Under the desktop app it is a child of the web application, started by
`sync_worker_lifespan`. Under Docker or systemd it is its own unit — set
`sync.supervise_worker: false`, or the application starts a second one and the
two race for the same jobs.
