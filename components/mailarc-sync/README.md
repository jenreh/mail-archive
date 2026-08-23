# mailarc-sync

Import orchestration: what gets fetched next, and what survives a restart.

The engine walks one account's mailbox through a mail source port, hands each
message to `mailarc_core.archive`, and checkpoints as it goes. The job queue is
the durable half — enqueue, lease, progress, cancel — because a first import is
measured in hours and a closing laptop lid is not an exception handler.

`jobs/scheduler.py` is the recurring *trigger* and sits beside the queue rather
than inside it: the queue decides who runs a job and what became of it, never
that one should exist. The scheduler enqueues and returns.

## Rules

- Depends on `mailarc-core` alone.
- **No provider.** `mailarc_google` and whatever follows it are registered in
  `app/composition.py`; the engine knows the port, never a vendor.
- **No Reflex, no `appkit` UI package.** A worker process has no browser.
- **No `runic.rag`.** Nothing a model invents may become ground truth.

`components/mailarc-core/tests/test_isolation.py` enforces the last three,
each from its own subprocess.
