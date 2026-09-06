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
| Sync | not a page | The circular arrow under the logo. One press asks every enabled mailbox for whatever it has not archived yet, and it spins for as long as anything is still importing — see [Importing mail](./importing-mail.md) |
| Search | `/` | The front door. Find a message by sender, recipient, date or words in it, and read it beside the results |
| Dashboard | `/dashboard` | What the archive holds — how many messages and mailboxes, when it last grew, what is queued and what is importing right now |
| Insights | `/insights` | What a rebuild derived from the archive, and the cross-check on it |
| Admin | popover | Three maintenance pages: **Mail accounts** (`/admin/accounts`), **Models** (`/admin/embedder`) and **Graph status** (`/admin/status`) |

Search is the main page because finding a message is what somebody opens a mail
archive to do; the dashboard is one click away rather than the other way round.

### Three ways to ask

The **Mode** switch at the top of the form decides how your words are read.

- **Fulltext** finds the words you type, in the subject and the body. It needs
  nothing configured and is the one that always works.
- **Semantic** finds mail *about* what you asked for, even where the message
  never uses your word. It needs an embedder — see
  [Semantic search](semantic-search.md) — and it reads the question and nothing
  else, so it greys the rest of the form out and says so.
- **Natural** takes a whole question — *"Nachrichten von meiner Hausverwaltung
  Santak"* — and **fills the fields below in from it**, then runs the ordinary
  search. It needs a model configured; see
  [Natural-language search](natural-language-search.md). The fields stay
  editable, which is the point: you can see what it understood and correct it.

Each of the last two is greyed out until the thing it needs is set up, with a
sentence beside the switch naming what to configure.

### The form asks one question at a time

Every field in the left column narrows the same answer: fill in two and you get
the messages that satisfy both, never the messages that satisfy either. The
form only ever makes the result list shorter.

**From** and **To** are pickers over the archive's own addresses rather than
text boxes, so you cannot ask for a sender no message ever had. Each list is
searchable — start typing and it narrows — and each offers the addresses in the
role it is for: **From** lists the addresses that actually sent something,
**To** the addresses that received it, whether in `To` or in `Cc`. Both lists
are in alphabetical order, like every other dropdown on the page. On a large
archive only the first hundred addresses are drawn before you type; typing
searches the whole list, so an address further down is reached by name rather
than by scrolling. A `Bcc` is not offered, because it is what the sender hid.

Both fields take several addresses, and they read them differently. **From**
means *any of these* — its label says so — and it can mean nothing else,
because a message has exactly one sender. **To** has a small **all / any**
switch beside its label: on **all** a message has to have reached every address
you picked, on **any** one of them is enough. It starts on **all**, like every
other field on this form.

Picking several addresses under one label is how you gather a person who writes
to you from more than one account.

The question box, both pickers, both dates and the account each clear in one
gesture — the small ✕ that appears once there is something to clear. **Reset**
below empties the whole form and goes back to the newest messages.

### The result list groups what it found

A **Group by** dropdown above the list decides how the hits are arranged. It
starts on **Conversation / Thread** and offers eight ways. **None** is first in
the dropdown because it is the off position; the other seven are alphabetical:

- **None** — a flat list. Worth choosing when the ranking of a full-text or
  semantic search is what you want to read straight down.
- **Conversation / Thread** — an exchange is one row: its newest message, with
  a chevron that opens the rest and a chip saying how many the conversation
  holds. Clicking the row opens the message it is showing; clicking the
  chevron opens the group. **A conversation is drawn whole**, including the
  messages that did not match: your own replies carry neither the sender you
  filtered on nor the words you searched for, and a thread missing one side of
  itself is not a thread. Those members are read with the page and are not
  counted as hits — the number over the list stays the number that matched.
  The chip therefore prints the conversation's size (`12`); it prints
  `1 of 12`, with a **Show whole conversation** button beside it, only when
  the archive could not complete the group — a thread longer than 200
  messages, or a graph that dropped the read.
- **Topic**, **Tag**, **Recurring group** — the message's derived topic, the tag
  you filed it under, or the set of people it keeps going to.
- **Subject**, **Sender**, **Receiver** — the subject with its `Re:` and `AW:`
  stripped, the address it came from, or the address it went to.

Only a conversation shows a message as its heading. Every other grouping draws a
labelled section — the sender's name, the topic's subject, `5 people · 8f3a…`
for a recurring group — with the matching messages indented under it, and a
message the archive could not file lands under **No topic**, **No tag**,
**No group** or **No recipient**. Clicking a section closes or opens it.

Topic and Recurring group read the derived layer, so on an archive that has
never been rebuilt every message sits under **No topic** or **No group** until
the first rebuild runs. Whatever the grouping, a group sits where its first hit
sat: grouping never moves a good hit down the page.

Mail imported before this feature existed groups less well if it came from
IMAP: the archive learned to thread a conversation's first message only later,
so an older exchange shows its opening mail as a row of its own beside its
replies, and the heading above them says `1 of 2` because the archive really
does hold two. **Graph status** (`/admin/status`) counts those messages and
has a **Repair conversations** button that files them. Nothing is fetched,
changed or deleted, and pressing it twice is harmless. Re-syncing the mailbox
does *not* fix it: the import skips every message it has already archived.

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
