# The import pipeline

[`mailarc_sync.engine.engine.ImportEngine`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-sync/src/mailarc_sync/engine/engine.py)
walks one mailbox through `MailSourcePort` and leaves it in the archive.

![The import pipeline](../diagrams/import-pipeline.svg)

There is no `pipeline.py` next to it. The engine **is** the pipeline; a second
module would only be somewhere for the same loop to be written twice.

Everything it writes with is handed in — the composition root knows how to build
a blob store, a graph session and a database session, and the engine only knows
the order to use them in. That is also what lets the tests run the real pipeline
against a directory of fixtures.

## The two decisions the code cannot state on its own

### Exactly one archive consumer

An `asyncio.Queue` sits between the fetch stage and the archive stage. It gives
backpressure: a slow graph slows the fetching down instead of filling memory.

Behind that queue there is **one** consumer, never two. Serialising FalkorDB
writes is cheaper than coordinating them, and a single writer is also what makes
the get-before-add in `MessageArchiver` sound.

### No message disappears quietly

A permanent failure travels the same queue as a success, as a `MessageFailure`
value, and is written down by the same consumer. Skipping a message therefore
*takes* a row in `mail_failed_messages` — not writing it down would mean
deleting code, not forgetting to add some.

There is no `except: pass` in this file and no place one would fit.

## Stage by stage

### 1. List

`source.list_messages(cursor, limit=batch_size)` returns one page of
`MessageRef`s and, maybe, a next cursor. Paging is the adapter's business; the
engine only asks whether a next cursor came back.

The first cursor comes from the checkpoint, so a resumed run starts where the
last one stopped.

### 2. Drop what we already have

One `IN (…)` per page against `mail_archived_messages`. This is the whole reason
that table exists — the graph cannot answer it for a batch, and asking per
message would be a round trip each.

A provider that lists the same id twice in one page is filtered here too. The
second copy would collide on the unique constraint rather than be recognised as
the one we just wrote.

### 3. Fetch

The page is cut into `fetch_concurrency` slices, each fetched under a semaphore.
The semaphore is held for the length of the stream, so **eight conversations is
the limit for this engine**, not for this page — which stays true if a caller
ever runs two mailboxes through one engine.

A slice is retried as a whole *minus what already arrived*:

```python
pending = [ref for ref in refs if ref.provider_message_id not in delivered]
```

Up to `MAX_FETCH_ATTEMPTS` (5) times. That number is not configuration: a
provider still refusing after five backed-off attempts is having an outage, and
a run that waits it out holds a lease it cannot honour.

Backoff is exponential from 1s, capped at 60s, with jitter added **upward only**
— the provider's own `Retry-After` is a floor the engine may exceed but never
undercut, and the jitter keeps a hundred slices that failed together from
returning together.

### 4. Parse and store

Off the event loop, in a thread: the stdlib email parser and the sha256 writes
both block.

The `.eml` bytes go to the blob store, then every attachment does — and the
attachment's payload is **dropped from memory** afterwards:

```python
self._blobs.put(attachment.payload, BlobKind.ATTACHMENT)
return attachment.model_copy(update={"payload": b""})
```

The file has a name on disk once that returns, and a page's worth of attachments
waiting in a queue is megabytes nobody reads — the writer keys the node on the
digest and never looks at the bytes.

A `MailPermanentError` becomes a value here rather than an exception. It is one
message's problem, and the stage that owns the database session is the one that
writes it down.

### 5. Archive

The consumer buffers to `batch_size`, then flushes. It flushes on that count
alone — never on "the queue happens to be empty", which reads like a latency win
and is the opposite: the fetch stage is network-bound, so the queue is empty most
of the time, and the consumer would open a FalkorDB driver and a SQLite
transaction *per message* instead of per batch.

**Order within a flush is deliberate.** Graph first, then the relational ledger.
A crash between the two leaves a message archived but not noted, so the next run
archives it again — and the writer is idempotent, so that costs one fetch and
nothing else. The other order would lose the message for good.

### 6. Checkpoint

Every `checkpoint_every` messages, and always when the mailbox runs out. It
advances only once everything before it is archived.

A finished walk stores `None`: there is no next page, and a later run starts from
the top and skips what it already has for the price of one listing pass.

## Why the consumer never raises out of its loop

This is not caution. A consumer that raises while the fetch stage is blocked on
a full queue **deadlocks the page**: the sentinel the fetch stage sends from its
`finally` has nowhere to go, the cancellation has already been delivered, and not
even an outer `asyncio.timeout` gets the run back.

So a write failure is remembered, the queue is drained, and the error is raised
once the page is over — before any checkpoint has advanced.

## Why the `TaskGroup` error is unwrapped

```python
except BaseExceptionGroup as failures:
    raise _first_error(failures) from failures
```

The error taxonomy is the engine's contract with its caller, and a caller that
has to unwrap an `ExceptionGroup` to find a `MailAuthError` will not. The group
stays the cause, so a second failure in another slice is still in the traceback.

## The error taxonomy

Three kinds, because an import loop only ever has three answers.

| Error | Answer | Where it is decided |
| --- | --- | --- |
| `MailAuthError` | Stop and ask the user for new credentials | Terminal for the job; the account goes to `auth_error` |
| `MailTransientError` | Wait and try the same call again | Retried per slice; `retry_after` is a floor for the backoff |
| `MailPermanentError` | Skip this one message and keep going | Becomes a row in `mail_failed_messages` |

Anything a provider adapter raises has to be one of these. **An adapter that
lets an `httpx` error escape has not decided** what the engine should do.

## The idempotent write

[`MessageArchiver.archive`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-core/src/mailarc_core/archive/writer.py)
writes one message and everything it points at, and is safe to repeat. Three
things carry that:

1. The canonical id is the same for both arrivals of one mail, so the `Message`
   lookup finds the first one.
2. Every other node is looked up before it is added.
3. `Session.relate` is a `MERGE`, so an edge already there is written again as
   itself.

An existing `Message` is left **exactly** as it stands. Its properties are
derived from the bytes and a re-parse computes the same values anyway, while the
fields the semantic phase fills in later are not ours to blank out.

### Nodes are resolved and flushed before any edge

An edge is a `MERGE` over two `MATCH` clauses. Relate a node that has not reached
the graph yet and **the edge is silently not written** — no error, just a missing
relationship. Hence one `session.flush()` between resolving the nodes and
relating them.

### `_NodeCache` exists because of one flush boundary

`Session.get` only sees an added entity after the flush, and a message routinely
carries the same address in `To` and in `Cc`. Without the map, the second lookup
would miss and create a duplicate node.

### No placeholder parent

If a reply is imported before the message it replies to, it keeps **no**
`REPLIES_TO` edge. Deliberately no placeholder: a `Message` holding nothing but
an id would be indistinguishable from a real one and would poison every count
and every analysis walking the graph.

The headers that would rebuild the link stay on the message and in the blob
store, so the loss is recoverable. A fabricated node would not be.

### Unit of work stays with the caller

The writer flushes, so the edges it writes can find their nodes. It never
commits, so a batch of messages shares one commit. Synchronous, because every
runic driver blocks — an async caller wraps the call in `asyncio.to_thread`.

## The knobs

| Setting | Default | Effect |
| --- | --- | --- |
| `batch_size` | 100 | Listing page size, and the archive write batch |
| `fetch_concurrency` | 8 | Concurrent fetch streams |
| `checkpoint_every` | 200 | Messages of listing work a crash can cost |

`batch_size` doing double duty is intentional: one page is one graph session and
one relational transaction.
