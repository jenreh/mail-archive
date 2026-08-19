# mailarc-sync

Import orchestration: what gets fetched next, and what survives a restart.

The engine walks one account's mailbox through a mail source port, hands each
message to `mailarc_core.archive`, and checkpoints as it goes. The job queue is
the durable half — enqueue, lease, progress, cancel — because a first import is
measured in hours and a closing laptop lid is not an exception handler.

Empty until phase 2 fills `engine/` and `jobs/`.

## Rules

- Depends on `mailarc-core` alone.
- **No provider.** `mailarc_google` and whatever follows it are registered in
  `app/composition.py`; the engine knows the port, never a vendor.
- **No Reflex, no `appkit` UI package.** A worker process has no browser.
- **No `runic.rag`.** Nothing a model invents may become ground truth.

`components/mailarc-core/tests/test_isolation.py` enforces the last two.
