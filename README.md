<div align="center">

# mail-archive

**Email archive and analysis**

![Version](https://img.shields.io/badge/version-1.0.0-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE.md)
[![Python](https://img.shields.io/badge/python-3.14%2B-orange)](https://www.python.org)
[![Reflex](https://img.shields.io/badge/reflex-0.9.5-purple)](https://reflex.dev)

[Getting Started](#project-initialization) • [Documentation](#documentation) • [Branching](#branching)

</div>

---

## Documentation

The full user and developer documentation lives in [`docs/`](docs) as a
VitePress site:

```sh
task docs:install     # once
task docs:dev         # http://localhost:5173
task docs:build       # docs/.vitepress/dist
task docs:diagrams    # regenerate the .drawio + .svg sources
```

Start at [`docs/index.md`](docs/index.md). It covers connecting a mailbox,
running an import, every setting, the component layering, the graph and
relational models, the import pipeline, the job queue, and what implementing a
new mail provider involves.

## What it does

- **Pulls a mailbox down over its own API** and keeps the original bytes on
  disk. Gmail, any IMAP host, and Microsoft 365, all behind one port.
- **Writes the graph email already carries** into a database you can query, with
  no guessing and no language model in the write path. Importing the same
  mailbox twice creates no new nodes and no new edges.
- **Searches it** by sender, recipient, date or words, and reads a message in a
  pane beside the results, grouped into conversations the way a mail client
  does — one row per exchange, expandable, and a button that pulls in the
  members the search itself did not return. With an embedder configured, it also
  searches for mail *about* something.
- **Derives what the headers imply**, in one job that throws the last answer
  away and computes it again: who gets written to together, which sets of people
  recur, which mail belongs to one piece of work and what that work is about,
  which mail is written again and again by a machine, and which correspondents
  form a circle.
- **Scores what probably matters** and names every term that produced the score,
  so the ranking is one you can argue with rather than a verdict.
- **Lets you tag mail and keep the tag.** A tag is the one grouping no rebuild
  and no mailbox clear-out can reach, and after each rebuild every tag is
  offered the untagged mail that looks like it belongs.
- **Draws one corner of the graph** at `/graph`, rooted at a topic, a circle, a
  tag, a person or a message, with an expandable canvas and a route between any
  two correspondents.
- **Serves the archive to a language model** over MCP, as ten read-only tools
  that answer at query time and never write back.
- **Runs on a laptop.** One SQLite file, a content-addressed blob store, and a
  graph server the desktop app carries with it.

## Project Initialization

Before initializing the project, install these required tools:

1. `uv`: [installation instructions](https://docs.astral.sh/uv/getting-started/installation/)
2. `task`: [installation instructions](https://taskfile.dev/installation/)

After both tools are installed, run:

```sh
task init
```

This installs the required Python version, syncs dependencies, and sets up pre-commit hooks.

## Project layout

A uv workspace: one Reflex application on top of first-party components.

```sh
app/                            the composition root, the configuration and the entry points
  composition.py                the only place that builds the core from configuration
  app.py                        rx.App, the lifespans, the publish_* calls, the page imports
components/mailarc-core/        everything that works without a browser
  src/mailarc_core/
    graph/                      the FalkorDB graph store
      model.py                  value objects (GraphServerStatus, GraphInfo, …)
      config.py                 GraphConfig — endpoint, backend, credentials
      runtime.py                finds the vendored redis-server + falkordb.so
      admin.py                  the FalkorDB-only commands runic does not cover
      client.py                 builds runic drivers and sessions (backend-independent)
      status.py                 reads a status snapshot; never raises when down
      server.py                 starts/adopts/stops FalkorDB (a no-op in remote mode)
    database/                   the relational store
      sqlite.py                 async/sync URL split and the SQLite pragmas
    archive/                    ground truth: what the import writes, and reads back
      model.py                  the runic nodes and edges, plus MessageSummary and Conversation
      writer.py                 MessageArchiver — the idempotent upsert into the graph
      blobs.py                  BlobStore — content-addressed originals on disk
      search.py                 SearchFilters and the hit/page value objects
      repository.py             MessageRepository / ThreadRepository — listing, search and conversations, via runic's query builder
      reader.py                 ArchiveReader — summaries out of the graph, bytes off disk
components/mailarc-sync/        the import engine, the job queue and the worker loop
components/mailarc-analytics/   derived nodes, analysis queries, embeddings
components/mailarc-google/      Gmail, behind the mail source port
components/mailarc-imap/        any IMAP mailbox — iCloud, an app password, a mail host
components/mailarc-m365/        Microsoft 365 over Graph, delegated or app-only
components/mailarc-mcp/         the read-only MCP tools — optional, behind the `mcp` extra
components/mailarc-ui/          the whole interface — pages, shell, kit, styles, states
scripts/                        build-time tooling (vendoring FalkorDB, icons)
src-tauri/                      the macOS desktop shell
```

The three provider components are siblings: none imports another, each hangs off
`mailarc-core` alone, and only `app/composition.py` is allowed to name one. That
is what lets a mailbox kind be added without touching the engine — see
[adding a mail provider](docs/developer/adding-a-provider.md).

Graph data is read and written through runic's OGM (`graph.session(config)`)
against its `GraphDriver` protocol, so `app.graph.backend` chooses the
database — FalkorDB, Neo4j, Memgraph, ArcadeDB or AGE. Only the server's
lifecycle and its Redis-level status are FalkorDB-specific, and `mode: local`
therefore requires it.

`app/` may import `mailarc_core`; the reverse is a bug, and
`components/mailarc-core/tests/test_isolation.py` fails if the core ever drags
in Reflex. Run the core on its own with `uv run python -m mailarc_core`.

## Running as a web server

```sh
task db:upgrade              # create .state/mail-archive.db and its tables
PROFILES=local task run      # http://localhost:8080 (frontend) + :3030 (backend)
```

It opens on the search page at `/`; the icon rail down the left edge holds the
dashboard (`/dashboard`), insights (`/insights`), the graph explorer (`/graph`)
and an **Admin** popover with mail accounts, embedder and graph status under
`/admin/`. There is no sign-in — the archive is a desktop application, and the
boundary is the machine it runs on.

The relational store is a single SQLite file at `.state/mail-archive.db` — no
database server to start. `task clean` wipes `.state/`, so it takes the
mailboxes, their credentials and the imported mail with it; the first
`task db:upgrade` after that gives the schema back, empty.

`PROFILES` picks which `configuration/config.<profile>.yaml` layers over
`config.yaml`. It is deliberately **not** set in `.env`: `appkit_commons` loads
that file with `override=True`, so a value there would beat the real
environment and pin every entry point to one profile.

| Profile | File | What it changes |
| --- | --- | --- |
| `local` | `config.local.yaml` | dev logging, ports 8080/3030, local FalkorDB |
| `prod` | `config.prod.yaml` | **single port 8080**, local FalkorDB |
| *(none)* | `config.yaml` | production logging, remote FalkorDB |

Only one profile set may be active per process — appkit's YAML reader caches and
merges in place, so a second merge with different profiles inherits the first.

For a cloud deployment use `PROFILES=prod` plus `APP__GRAPH__MODE=remote` and
`APP__GRAPH__HOST=<your falkordb>`.

## Running as a desktop app (macOS)

The desktop shell lives in [`src-tauri/`](src-tauri) and bundles its own
FalkorDB and `uv`, so **the built `.app` needs no Homebrew, no Docker, no Redis
and no `uv` on the target Mac**. A GUI launch inherits launchd's PATH
(`/usr/bin:/bin:/usr/sbin:/sbin`), which has no developer toolchain in it at
all, so anything the app needs has to travel with it.

```sh
task tauri:init     # check the toolchain, install tauri-cli, generate icons
task tauri:vendor   # download + build the vendored runtimes (FalkorDB, uv)
task tauri:dev      # run the desktop app
task tauri:build    # produce build/release/bundle/macos/mail-archive.app
```

`task tauri:build` also runs `task tauri:frontend`, which compiles the Reflex
frontend up front so the app's first launch does a few seconds of rebuild
rather than a cold `reflex init`.

A **release** launch keeps the mail per user, out of this checkout:

```text
~/Library/Application Support/de.rehpoehler.mailarc/
├── mail-archive.db      mailboxes, credentials, jobs, checkpoints
├── mailstore/           the original bytes — this is the archive
└── falkordb/            the graph's data
```

The path is derived from the bundle identifier in `tauri.conf.json`, the shell
creates it `0700` before the backend starts, and it hands the backend
`app_database_url_override`, `app_archive_store_dir` and `app_graph_data_dir`
pointing into it. `task tauri:dev` and every other development run set none of
them and stay on `.state/`, so the two never share an archive. Back the
directory up by quitting the app and copying it.

Still **not** self-contained: the backend itself runs from this checkout. See
`task tauri:bundle:sidecar` for what freezing it would take.

Build-machine only requirements: Xcode command line tools, Rust, Node, Homebrew
`openssl@3` (its dylibs get copied into the bundle and repointed at
`@loader_path`) and Homebrew `librsvg`, which renders the app icons from
`docs/public/favicon.svg`.

### What `task tauri:vendor` produces

| File | Where it comes from |
| --- | --- |
| `redis-server` | built from the pinned Redis source with `BUILD_TLS=no`, so it links only system libraries |
| `falkordb.so` | the pinned official FalkorDB release asset, SHA256-verified |
| `libssl.3.dylib`, `libcrypto.3.dylib` | copied from Homebrew, because the FalkorDB module links them at absolute paths |
| `uv` | the pinned official `uv` release binary, SHA256-verified |

Every rewritten Mach-O is re-signed — editing a Mach-O invalidates its signature
and arm64 macOS refuses to load unsigned code. The run ends by re-reading the
load commands and failing if anything still points outside `@loader_path`,
`/usr/lib` or `/System`:

```sh
task tauri:vendor:verify
```

To prove the bundle really is self-contained, `brew unlink openssl@3` and launch
the built `.app`. The build machine has Homebrew and will otherwise mask a
broken bundle.

### Known limitation

The bundled `.app` still runs the **Python** backend from this checkout via
`uv`; only FalkorDB is fully vendored. Freezing the backend is stubbed out —
see `src-tauri/src/sidecar.rs` and `task tauri:bundle:sidecar`.

## Branching

See [BRANCHING.md](BRANCHING.md) for the repository branch naming policy.
