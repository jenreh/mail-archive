# The MCP server

`mail-archive-mcp` is a console script that serves the archive to an MCP client
over stdio. Ten read-only tools, every one of them a named, parameterised
statement out of `mailarc_analytics.queries.catalog` — **no tool takes a query
string that reaches the graph**.

## It is optional, and that is the point

The server is `components/mailarc-mcp/`, a workspace member the root project
declares as an **extra**:

```toml
[project.optional-dependencies]
mcp = ["mailarc-mcp"]
```

`fastmcp` is around sixty distributions — `keyring`, `cryptography`, `authlib`,
`pyjwt`, `uvicorn`, `watchfiles`, `websockets` — and a desktop archive uses none
of them. So the two deployments differ by one flag:

| | Command | Distributions |
| --- | --- | --- |
| Desktop bundle | `uv sync` | 82 |
| Web deployment | `uv sync --extra mcp` | 125 |

`task tauri:deps` prints both and fails if `fastmcp` ever resolves into the
first. Nothing has to be switched off for the desktop path: the Tauri shell
starts the backend with plain `uv run reflex run`, which requests no extras.
The `Dockerfile` syncs `--all-extras`, so the web image picks the server up
already.

**A developer environment is the web deployment.** `task install` syncs
`--extra mcp`, because the test suite covers the component.

### What has to stay true

`app/mcp_server.py` is a thin entry point — it configures the process, builds
the five readers out of `app/composition.py`, and hands them to the component's
`build_server`. **Nothing else under `app/` may import it, or `mailarc_mcp`, or
`fastmcp` at module level**, or the web application and the worker stop starting
on an installation that left the extra out. `tests/test_mcp_server.py` reads
every module in `app/` and checks.

The split is also why `build_server(access, version=…)` takes both arguments and
has no defaults: a component may not turn configuration into an object, so the
readers and the version arrive from the composition root
([architecture](./architecture.md#the-composition-root)).

## The tools

| Tool | Answers |
| --- | --- |
| `search_messages` | Full text over subject and body, or — with `semantic=true` — a vector search |
| `co_recipients` | Which two addresses keep being written to together |
| `topics` | Groups of messages one analysis thinks belong to the same work |
| `templates` | Texts written again and again with barely a word changed |
| `thread` | The conversation one message belongs to, oldest first |
| `timeline` | The archive by date, newest first |
| `important_messages` | The mail that probably matters, best first, with the reasons why |
| `topic_messages` | One topic's mail, most important first, with a preview of each |
| `tags` | The names a person filed their own mail under |
| `tagged_messages` | The mail wearing one tag, newest first |

`co_recipients`, `topics`, `templates` and `important_messages` read the derived
layer, so they answer only after a `derive` job has run. They say so rather than
returning an empty list: "no topics" would read to a model as "this archive
holds no projects".

### The four analysis tools

They are registered by `_analysis_tools(mcp, access)` rather than in the body of
`build_server`, which was already the longest thing in the module. Registration
and nothing else happens there; every decision that makes the server what it is
stays in `build_server`.

**`important_messages` returns the reasons, and that is the point.** The score
is arithmetic over headers — who answered, whether the owner was written to
directly rather than copied, how central the sender is, how few recipients there
were, whether the text looks automated — and every term that fired is named in
`reasons`. A model handed a bare number would have nothing to qualify it with.
The whole archive is scored by one rebuild, so unlike a search score these
numbers are comparable with each other.

**`topic_messages` is the §3.2-compliant way to have a topic summarised.** It
returns the members most important first, each with subject, sender, date and a
preview, which is enough to say what a topic is about without pulling forty full
bodies into a context window. Whatever the model concludes stays in its answer.
Nothing it writes is stored on the topic or anywhere else, because a summary
that became part of the graph would turn a guess into a recorded fact.

A `topic_id` is a digest of its members and every rebuild mints a new one, so an
id a caller wrote down earlier fails with an explanation rather than answering
with nothing. `tagged_messages` fails differently on purpose: an unknown tag id
is an error, while a tag that exists and holds nothing is an empty list, because
those are two different states and only one of them is the caller's mistake.

**`tags` has no limit to clamp.** The population is not the archive, it is the
handful of names one person typed.

**Nothing on this server writes a tag.** `ArchiveAccess.tags()` hands back the
same `TagStore` the pages write through, and this process only ever reads it.
Two stores would be two answers to "where are the tags".

There is deliberately **no subgraph tool**. A dump of nodes and edges is a
picture, and a picture costs a model tokens to reconstruct what the listings
already say in sentences. [The graph explorer](./graph-explorer.md) is where a
person looks at one.

## Wiring a client

The script is installed with the application — the entry point is metadata on
the root wheel, so it exists whether or not the `mcp` extra does; run from a
checkout without the extra it exits with a sentence naming the flag rather than
a traceback. From a wheel it currently does not get that far: `app/__init__.py`
calls `configure()` at import and the wheel ships no `configuration/`, so an
installed copy fails earlier with a configuration error. That is a packaging
gap rather than an MCP one — it is the same for `python -c "import app"` — but
it means the friendly sentence is a checkout's behaviour, not an installation's.
The command is whatever your environment resolves `mail-archive-mcp` to. For
Claude Desktop or any client reading the same JSON shape:

```json
{
  "mcpServers": {
    "mail-archive": {
      "command": "/absolute/path/to/mail-archive-mcp"
    }
  }
}
```

From a checkout, `uv run mail-archive-mcp` works and picks up the same
configuration the application does — the server is its own process and reads
`configuration/` and the `app_*` environment exactly as `app/worker.py` does.

To try it without a client, `fastmcp` can drive the server in memory, which is
how the tests do it:

```python
from fastmcp import Client

from app.mcp_server import archive_access, version
from mailarc_mcp import build_server

async with Client(build_server(archive_access(), version=version())) as client:
    print(await client.call_tool("timeline", {"limit": 5}))
```

A test builds its own `ArchiveAccess` instead, with five factories of its own —
`components/mailarc-mcp/tests/test_mcp_server_local.py` points them at a
vendored FalkorDB holding six real messages.

## The trust model, stated plainly

**The server has no gate of any kind.** Any process on the machine that can
spawn `mail-archive-mcp` reads every message, sender, thread and correspondent
pair in the archive, with no prompt and no credential.

That is a deliberate choice and a defensible one for a single-user desktop
archive: the boundary is the operating-system process, and anything that can
run the script can already read the archive directory itself. The UI draws the
same line rather than a stricter one — it has no sign-in either, so whoever
opens the window reads every mailbox. One boundary, stated once, is easier to
reason about than two that disagree.

Two consequences follow:

- Every tool declares `readOnlyHint=true`, which invites a compliant client to
  call it **without asking its user**. That is right for a tool that cannot
  write and wrong the moment the transport stops being a local process.
- `main()` hard-codes `transport="stdio"`. Adding a network transport would
  need a real authentication story first — not a flag.

Nothing a model concludes is written back. The archive has no path by which a
tool could add, change or delete a node, and `runic.rag` is banned
repository-wide with a subprocess test proving it.

## What crosses the wire on a failure

Three bands, and only one of them carries an exception's own words:

- A `SemanticError` — "no embedder is configured", "run the embed job", a
  dimension mismatch — is shown **verbatim**, because those sentences were
  written for a person and name the setting to change.
- An upstream refusal (`MailError`) gets a fixed sentence. Its own text quotes
  the provider's error body and the resolved base URL, which on a corporate
  installation is an internal hostname.
- Anything else gets a fixed sentence too: a driver's message can name a path
  inside the installation, and a tool result is read verbatim by a model.

Both fixed sentences are logged with their detail first, so nothing is lost to
whoever has to fix it. `mask_error_details=True` is on as a backstop, but it is
only a backstop — it protects the protocol response, not the server's own log
stream, which for a stdio server the client captures. An exception escaping a
tool body is treated as a defect.

## Only `fastmcp` is imported

`mcp` — the protocol package — is in the lock file only because `fastmcp`
resolves to `fastmcp-slim[client,server]`, which requires it. No manifest in
this workspace declares it, so nothing here imports it: `fastmcp` re-exports the
classes a server needs, and `fastmcp.tools.base.ToolAnnotations` *is*
`mcp.types.ToolAnnotations`, the same class object. Two tests assert it — one
over the component's three modules, one over the entry point — because the
failure mode is a console script that dies at import and an MCP client showing a
blank error.
