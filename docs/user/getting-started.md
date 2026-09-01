# Getting started

## What you need first

Two tools, and nothing else:

| Tool | Why | Install |
| --- | --- | --- |
| [`uv`](https://docs.astral.sh/uv/getting-started/installation/) | Python version, virtualenv, dependencies | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [`task`](https://taskfile.dev/installation/) | Every command in this project is a task | `brew install go-task` |

`uv` installs Python 3.14 itself, so you do not need one on the machine.

Building the macOS desktop app needs more — Xcode command line tools, Rust,
Node and Homebrew `openssl@3` — but only for the build. See
[the desktop app](desktop-app.md).

## Set the project up

```sh
task init
```

That installs Python 3.14, pins it, syncs every dependency across the
workspace, and installs the pre-commit hooks.

## Create the database

```sh
task db:upgrade
```

The relational store is a single SQLite file at `.state/mail-archive.db` —
there is no database server to start. This creates the file and its tables.

> `task clean` wipes `.state/` and takes the mailboxes, their credentials and
> every job with it. The next `task db:upgrade` gives you the schema back,
> empty; the mail itself is gone.

## Run it

```sh
PROFILES=local task run
```

- Frontend: <http://localhost:8080>
- Backend: <http://localhost:3030>

Two things start alongside the web application, both owned by its ASGI
lifespan:

- **FalkorDB**, from the vendored binaries under `src-tauri/resources/falkordb`.
  If they are not there yet, run `task tauri:vendor` — or point the app at a
  graph someone else runs, see [configuration](configuration.md).
- **The import worker**, as a child process (`python -m app.worker`). It is
  what actually runs an import. Turn it off with `sync.supervise_worker: false`
  where something else already runs it.

Neither failing takes the application down. **Graph status**, under **Admin**
in the rail, shows the graph server's state including why it did not start.

There is no sign-in. The archive is a desktop application, so the window opens
straight onto the page you work in.

## Finding your way around

The left edge is a slim icon rail; every icon names itself when you hover it.

| Icon | Where | What it is |
| --- | --- | --- |
| Search | `/` | The front door. Find a message by sender, recipient, date or words in it, and read it beside the results |
| Dashboard | `/dashboard` | What the archive holds — how many messages and mailboxes, when it last grew, what is queued and what is importing right now |
| Insights | `/insights` | What a rebuild derived from the archive, and the cross-check on it |
| Admin | popover | Three maintenance pages: **Mail accounts** (`/admin/accounts`), **Embedder** (`/admin/embedder`) and **Graph status** (`/admin/status`) |

Search is the main page because finding a message is what somebody opens a mail
archive to do; the dashboard is one click away rather than the other way round.

## Prepare the graph schema

The graph needs its indexes before an import is worth running:

```sh
task graph:upgrade
```

This creates the full-text index over `subject` and `body_text`, range indexes
on the four properties the analyses group by, and the unique constraint on
`rfc_message_id` that makes a re-import a no-op. Check it landed with
`task graph:current`.

## Where everything ends up

Running from this checkout — `task run`, `task tauri:dev`, the test suite —
everything lands under `.state/`:

```text
.state/
├── mail-archive.db      SQLite — mailboxes, credentials, jobs, checkpoints
├── mailstore/           the original .eml bytes and attachments, by sha256
└── falkordb/            the graph's own data directory
```

The **built macOS app** keeps the same three things per user instead, out of
the checkout entirely:

```text
~/Library/Application Support/de.rehpoehler.mailarc/
```

Same names inside, and the directory is created `0700` so no other account on
the Mac can read it. See [the desktop app](desktop-app.md#where-it-keeps-your-mail).

One directory either way. Stop the application, back that directory up, and you
have backed up the archive.

## What next

- [Connect a mailbox](connecting-a-mailbox.md) and import from it.
- [Configuration](configuration.md) if you want it pointed somewhere else.
- [Architecture](../developer/architecture.md) if you are about to change code.

## The commands you will actually use

```sh
task                     # list every task
task run                 # the web application (add PROFILES=local)
task sync:worker         # the import worker on its own, in the foreground
task db:upgrade          # relational migrations
task graph:upgrade       # graph schema migrations
task test                # pytest with coverage
task format lint typecheck
task clean               # wipe .state/, .web/, caches — destroys the archive
```
