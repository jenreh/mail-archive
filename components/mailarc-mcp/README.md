# mailarc-mcp

The archive as an MCP server: ten read-only tools a language model can call.

§7.5's answer to "can I ask my archive questions", and §3.2's line drawn under
it — the answer is a model *reading* the archive, never one writing to it.
Every statement that reaches the store is a named constant in
`mailarc_analytics.queries.catalog` or a query-builder statement compiled
against the mapped models, and **no tool takes a query string that reaches the
graph**. A model can ask the ten questions the tools express and no other.

## Why it is a component of its own

`fastmcp` is around sixty distributions — `keyring`, `cryptography`, `authlib`,
`pyjwt`, `uvicorn`, `watchfiles`, `websockets` — and the desktop bundle has no
use for a single one of them. Splitting the server out of `app/` makes that
tree an **extra** the root project declares (`uv sync --extra mcp`) rather than
a dependency every installation carries: the web deployment asks for it, the
Tauri bundle does not, and the difference is one flag instead of a fork.

## Layout

```
server/
  model.py     what a tool answers with — the contract a model reads, kept
               apart from the component row types so an internal rename is
               not a published breaking change. No I/O.
  reads.py     ArchiveAccess: where an answer comes from. Holds five
               factories it was handed and builds none of them itself, plus
               the two graph reads no reader above it hands out — one
               conversation, and one topic's members.
  server.py    the ten tools, the failure translation, and the FastMCP
               logging fix a stdio process needs.
```

There is no `config.py`, and that is the point: this component reads nothing
out of a settings file. `build_server(access, version=…)` is handed everything
it needs, because a component may not turn configuration into an object — see
below.

## Rules

- Depends on `mailarc-core` and `mailarc-analytics`, plus `fastmcp`. Never
  `mailarc-sync`, never `mailarc-google`: a tool answers questions about an
  archive that already exists and has no business knowing how mail got into it.
- **No `app`.** `ArchiveAccess` is constructed with five factories — a graph
  session, the analytics reader, the archive reader, the semantic search and
  the tag store — and asks each one on first use. `app/mcp_server.py` is the module that binds
  them to `app/composition.py`, which is the only place in the repository
  allowed to build a component from configuration. Before the split this
  package reached into the composition root directly, which made it un-movable.
- **No Reflex, no `appkit` UI package.** A tool result is a value; nothing here
  renders.
- **No `runic.rag`.** The graph is already exact.
- **Only `fastmcp`, never `mcp`.** The protocol types live in `mcp`, and
  `fastmcp` re-exports the ones a server needs — `fastmcp.tools.base.
  ToolAnnotations` *is* `mcp.types.ToolAnnotations`, the same class object.
  Importing `mcp` directly would be a dependency on an intermediary's
  resolution: no manifest in this workspace declares it, and the day `fastmcp`
  vendors or drops it a *console script* dies at import and an MCP client shows
  a blank error.
- **Nothing writes to stdout.** The JSON-RPC frames own it, and one stray line
  makes the client drop the message it lands in.

`components/mailarc-core/tests/test_isolation.py` enforces the UI ban, the
`runic.rag` ban and the two sibling bans, and skips this component when the
`mcp` extra is not installed. `tests/test_mcp_package.py` enforces the rest.

## Three rules the tools follow

**Every expected failure is a `ToolError`.** No embedder configured, nothing
derived yet, an unknown message id, a graph that does not answer — each is a
state a person can fix, and each comes back as a sentence naming the fix.

**No empty result stands in for a failure.** An empty list means the archive
holds nothing matching; every other reason for an empty answer raises. A user
who reads "no results" stops looking.

**Every limit is clamped.** The caller is a model that cannot know this
archive's size and will ask for 10 000 rows. Clamping answers the question it
meant to ask instead of spending a round trip teaching it a bound.

## The tools

`search_messages`, `timeline` and `thread` read the ground truth; `topics`,
`co_recipients`, `templates` and `important_messages` read what a rebuild
derived from it; `topic_messages` reads one topic's mail so that a model can
summarise it in its own answer, and `tags`/`tagged_messages` read the
annotation layer a person builds by hand — the only grouping here that
survives a rebuild.

There is deliberately **no subgraph tool**. A dump of nodes and edges is a
picture, and a picture costs a model tokens to reconstruct what the listings
already say in sentences; the graph explorer in `mailarc-ui` is where a *person*
looks at one.

Nothing here writes a tag, promotes a cluster or stores a summary. §3.2:
email already carries an exact graph in its headers, and a model's conclusion
is not allowed to become part of it.
