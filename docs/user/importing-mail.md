# Importing mail

## What an import does

It walks the mailbox one page at a time. For every message it has not seen
before it fetches the original RFC 5322 bytes, writes those bytes to disk under
their sha256, parses them, and writes the result into the graph.

![The import pipeline](../diagrams/import-pipeline.svg)

The first import of a large mailbox is tens of thousands of HTTP requests and
takes hours. It is built to survive that: the laptop lid can close, the process
can be killed, the provider can rate-limit — and the next run picks up where the
last one stopped.

## Starting one

An import is a **job**. Something enqueues it, a worker claims it, and the job
row is what you watch.

The worker runs as a child of the web application by default. Where you want it
on its own — under Docker, systemd, or just to watch its log:

```sh
task sync:worker
```

Set `sync.supervise_worker: false` in that case, or the application starts a
second one and the two race for the same jobs.

## Reading the progress

A job carries four numbers, and they are deliberately not collapsed into one:

| Number | Meaning |
| --- | --- |
| `listed` | The provider offered this message |
| `skipped` | We already had it — no fetch, no write |
| `archived` | It reached the graph |
| `failed` | It did not, and there is a row saying why |

The progress bar counts `archived + skipped` as done, because that is how much
of the mailbox has been dealt with. `failed` is tracked separately on purpose:
folding it into "archived" would make a broken mailbox look like a small one.

`estimated_total` is the provider's own guess. It is allowed to be wrong, and on
Gmail it usually is by a little.

## Cancelling

A job is **asked** to stop, never killed. The request is read between two
batches, so a cancel takes effect once the batch in flight has been written —
typically within a page. Nothing is left half-written, and the checkpoint is
current, so resuming continues from there.

## Resuming, and what a crash costs

Two mechanisms, doing different jobs.

**The checkpoint** stores the provider's cursor every ~200 messages. A crash
costs at most that many messages of *listing* work.

**The archived-message ledger** remembers every provider id already written. So
even the messages between the last checkpoint and the crash are not re-fetched
— the next run lists them, recognises them, and skips them.

The two orders in the write path are chosen for this:

- The graph is written **before** the ledger. A crash between them leaves a
  message archived but not noted, so the next run archives it again — which is
  free, because the writer is idempotent. The other order would lose the message
  for good.
- The checkpoint advances **after** everything before it is archived. It never
  points past work that did not land.

## Importing the same mailbox twice

Costs one listing pass and writes nothing.

A message's identity comes from the message, not from the provider: its RFC 5322
`Message-ID` where there is one, and a content hash over
`sent_at | from | subject | sha256(body)` where the sender omitted it. So the
same mail arriving through two of your accounts is **one** `Message` node with
two `ARCHIVED_FROM` edges — not two copies.

The same holds for everything the message points at. One `Address` node per
address, one `Attachment` node per file content (twenty messages carrying the
same PDF share one node, and the filename hangs on the edge, because senders
rename things).

## When a message is skipped

Some messages will not parse — broken MIME, a truncated multipart, a 404 from
the provider. Those are skipped, and **every skip leaves a row** in
`mail_failed_messages` with the provider's id, a reason and the detail.

There is no silent failure anywhere in the import. A skipped message is a
countable event, not a gap.

## When the provider pushes back

A rate limit or a 5xx is treated as "the same call may work". The engine backs
off exponentially — 1s, 2s, 4s, … capped at 60s, with jitter added upward so a
hundred slices that failed together do not all return at the same instant. If
the provider sent a `Retry-After`, that is a floor the engine will not undercut.

After five attempts on the same slice, the engine gives up. That is not a
hiccup, it is an outage, and a run that waits it out would hold a lease it
cannot honour. The job fails, goes back in the queue, and a later run resumes at
the checkpoint.

An **authentication** failure is different: it ends the job immediately and puts
the account into `auth_error`. Retrying a revoked token never works.

## What the graph ends up holding

![Graph ground truth](../diagrams/graph-model.svg)

Everything above is read out of the message or computed from it
deterministically, so re-parsing the same bytes gives the same node. Nothing is
guessed.

The graph keeps a capped rendering of the body — 64 KB by default, enough for
full-text search. The whole message stays in the blob store, so a longer body is
never lost, only unindexed past that point. A parser fix can be replayed over
the entire archive from those bytes without asking the provider for anything
twice.

## Looking at what arrived

**Review** in the navigation (`/mail/review`) lists the archive the way a mail
client would: sender and date on the first line, subject and a paperclip on the
second, two lines of preview under them, newest first. Under the preview sit
the labels the provider filed the message under — your own labels in blue,
folders in teal, the provider's housekeeping (Inbox, Updates, Unread) in grey
and last. Pick a message and the right half shows it in two tabs:

- **Message** — the way a mail client renders it: subject, From / To / Cc /
  Date, the attached files with their sizes, and the body. An HTML mail keeps
  its layout and its inline pictures; a plain-text mail shows as text.
- **Source** — the **raw bytes** the provider handed over, headers and all,
  read back from the blob store.

A few things are on purpose:

- The list is read from the graph, the original from disk; nothing is fetched
  from the provider. What you see is what the import wrote.
- The rendered body sits in a sandboxed frame that may load **nothing
  remote** by default — no scripts, no tracking pixels, no fonts from a
  stranger's server — the same default a mail client ships with. Only the
  pictures the mail itself carries are shown. When a message wants remote
  content, a bar above the body says so and offers two choices: **Allow
  once** opens this one rendering, **Allow for this sender** records the
  decision on the sender's address in the graph, so every later message from
  that exact address opens with its pictures. Scripts stay blocked either
  way — trust extends to being seen, never to being run.
- The list brings in a hundred messages at a time; **Load more** at the bottom
  appends the next hundred. The count in the header says how far you are.
- A very large source is cut after the first 256 KB, with a note. The rest is
  still on disk; the viewer just declines to render a PDF as base64.
- A message archived without a stored original — or whose blob has since
  gone missing — says so instead of erroring.

Like the accounts page, the review is **admin-only**: the archive is every
mailbox of the installation.

## Job kinds

| Kind | State |
| --- | --- |
| `import` | Works — a full walk of a mailbox |
| `incremental` | Planned. Gmail's `historyId` delta |
| `derive` | Planned. Rebuilds the analysis nodes |
| `embed` | Planned. Fills in message embeddings |

A job of an unimplemented kind fails with "no handler registered", which is both
what the worker does with an unmapped kind and the truth.
