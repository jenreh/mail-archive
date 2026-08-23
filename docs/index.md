---
layout: home

hero:
  name: "mail-archive"
  text: "An email archive you own"
  tagline: >-
    Pull a mailbox down over its own API, keep the original bytes on disk, and
    write the graph email already carries into a database you can query.
  actions:
    - theme: brand
      text: Getting started
      link: /user/getting-started
    - theme: alt
      text: Architecture
      link: /developer/architecture
    - theme: alt
      text: View on GitHub
      link: https://github.com/jenreh/mail-archive

features:
  - title: Ground truth, not guesswork
    details: >-
      Email headers are already an exact graph — senders, recipients, threads,
      labels, attachments. Nothing infers it, so nothing can get it wrong.
  - title: Import twice, write once
    details: >-
      A message's identity comes from the message. Re-importing a mailbox
      creates no new nodes and no new edges, and a crash costs one fetch.
  - title: Nothing disappears quietly
    details: >-
      Every skipped message leaves a row saying why. There is no "except: pass"
      anywhere in the import path, and no place one would fit.
  - title: Runs on a laptop
    details: >-
      A single SQLite file, a content-addressed blob store, and a graph server
      the desktop app carries with it. No Docker, no Homebrew, no Redis.
---

## Start here

**Using it**

| Page | What it answers |
| --- | --- |
| [Getting started](./user/getting-started.md) | Install the tools, create the database, get the app running |
| [Connecting a mailbox](./user/connecting-a-mailbox.md) | Add an account, get through Google's consent screen |
| [Importing mail](./user/importing-mail.md) | Start an import, read the progress, cancel, resume, review what arrived |
| [Semantic search](./user/semantic-search.md) | Turning an embedder on, what it costs, and what an assistant may read |
| [Configuration](./user/configuration.md) | Profiles, environment variables, every setting there is |
| [The desktop app](./user/desktop-app.md) | Building the macOS `.app`, what it bundles, what it does not |
| [Troubleshooting](./user/troubleshooting.md) | The failures that actually happen, and what each one means |

**Building on it**

| Page | What it answers |
| --- | --- |
| [Architecture](./developer/architecture.md) | The six components, who may import whom, and why |
| [Data model](./developer/data-model.md) | The graph nodes, the six tables, the blob store |
| [The import pipeline](./developer/import-pipeline.md) | Stage by stage, and the two decisions that shape it |
| [Jobs and the worker](./developer/jobs-and-worker.md) | Leases, claims, cancellation, crash recovery |
| [Adding a mail provider](./developer/adding-a-provider.md) | What implementing `MailSourcePort` involves |
| [The MCP server](./developer/mcp-server.md) | The six read-only tools, wiring a client, and the trust model |
| [Testing](./developer/testing.md) | Where tests live, what the markers mean, the isolation test |
| [Operations](./developer/operations.md) | Migrations, tasks, the quality gate |

## Why there is no LLM in the write path

Email already carries an exact graph in its headers. An LLM extraction would
only lay a probabilistic layer over ground truth and make every count
approximate — so nothing writes to the archive except the import. A model reads
it at query time, through the MCP server (`mail-archive-mcp`), and never writes.

That rule is enforced, not merely stated: no component may import `runic.rag`,
and a test checks it from a subprocess.

## Where the project stands

Built and tested:

- The domain — parsing, canonical identity, the error taxonomy, the port.
- The archive — the graph writer, the blob store, idempotent re-import.
- The relational store — six tables, migrations, repositories.
- The import engine and the job queue, with a real second port implementation
  (a folder of `.eml` files) driving them.
- The graph server's lifecycle, status and vendored runtime.
- The Gmail adapter end to end — consent, credential refresh, the HTTP client
  and the mapping from Google's JSON into the domain, registered as a provider
  an account can pick, with pages that mount the account, import and review
  states.
- Two more mail providers behind the same port: **IMAP** — any host, over TLS,
  with a `UIDVALIDITY`/`UIDNEXT` cursor and no consent step at all — and
  **Microsoft 365** over Graph, delegated or app-only, whose cursor is a whole
  `deltaLink`. Neither cost a line in the engine, the core or the UI.
- The three deterministic analyses — who is written to together, which mails
  belong to one project, which mails are written again and again with the same
  wording — and `task graph:rebuild-derived`, which throws all three away and
  computes them again.
- The insights page, which starts that rebuild as a queued job, watches it, and
  reads the findings back — including a cross-check that recomputes the
  co-addressed counts from the messages and holds them against the edge a
  rebuild wrote, so a wrong write path says so instead of being reported as a
  finding.

- Semantic search and the MCP server. An embedder is optional and off by
  default — see [Semantic search](./user/semantic-search.md) — and with one
  configured, the `embed` job fills in `Message.embedding`, the insights page
  gains a search box, A2 gains its sixth signal, and `mail-archive-mcp`
  answers six read-only tools over the same query catalogue.

Not there yet:

- **Incremental sync and a schedule.** Every import is still a full pass over
  the scope it is given.

The full plan lives in
[`spec/mail-import-and-analysis.md`](https://github.com/jenreh/mail-archive/blob/main/spec/mail-import-and-analysis.md)
(German). Section numbers quoted in the source — §7.3, §8.1 — refer to it.

## The diagrams

Every picture in these pages is generated from one description, so the
`.drawio` and the `.svg` beside it cannot disagree:

```sh
uv run python docs/diagrams/build.py
```

See [the diagram sources](./diagrams/) for how to edit one.
