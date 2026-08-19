# mailarc-core

The part of mail-archive that has nothing to do with a browser.

`mailarc_core` owns the FalkorDB graph server — finding the vendored binaries,
starting and stopping the process, and reading its status — plus the SQLite
wiring the application's single database URL cannot express. A CLI, a worker or
a test can use all of it without Reflex being installed.

Graph *data* is reached through [runic](https://pypi.org/project/runic-py/)'s
OGM: `graph.session(config)` hands out a `runic.ogm.Session`, so queries are
written against mapped `Node`/`Edge` models instead of hand-rolled Cypher.
Drivers are held as runic's `GraphDriver` protocol, never as one vendor's
class, and `GraphConfig.backend` picks which database answers — `falkordb`,
`neo4j`, `memgraph`, `arcadedb` or `age`.

The server's *lifecycle* is not runic's business and stays here. It is also
the one part that cannot be backend-independent: the vendored binaries are a
redis-server, so `mode: local` is FalkorDB and only FalkorDB. Every other
backend is reached with `mode: remote`, and the config refuses the
combination that cannot work.

## Layout

One package per store, one module per concern.

### `graph/` — the graph store

| Module       | Job | Backend-independent? |
| ------------ | --- | --- |
| `model.py`   | Value objects: `GraphBackend`, `GraphServerMode`, `GraphInfo`, `ServerMetrics`, `GraphServerStatus`. No I/O. | yes |
| `config.py`  | `GraphConfig` — where the server is, which backend answers, and how it is run. | yes |
| `runtime.py` | Finds and validates the vendored `redis-server` + `falkordb.so`. | FalkorDB |
| `admin.py`   | PING, `INFO`, `MODULE LIST`, `GRAPH.LIST`, and the connection `FalkorDBDriver.close` forgets. | FalkorDB |
| `client.py`  | `session(config)`, `connect(config)`, `close(driver)` — the only place a driver is built. | yes |
| `status.py`  | `read_status(config)` — a snapshot; never raises for an unreachable server. | mostly |
| `server.py`  | `FalkorDBServer` — starts/adopts/stops a local server; a no-op in remote mode. | FalkorDB |

The modules are layered in that order: `model` imports nothing else here,
and no module in the package imports `server` — only the package root and
`python -m mailarc_core` do.

`admin.py` is the deliberate exception to the protocol rule. Everything in it
is a Redis command runic has no opinion about, so it is quarantined in one
named module and every caller checks `config.backend` first. `client.py` then
has no FalkorDB in any signature, and `status.py` reads the Redis-only facts
through `admin` and simply leaves them empty for other backends.

### `database/` — the relational store

| Module       | Job |
| ------------ | --- |
| `sqlite.py`  | Async/sync URL split, directory creation, and the WAL/foreign-key/busy-timeout pragmas. |

The graph names are re-exported from the package root, so the application
writes `from mailarc_core import FalkorDBServer`; the SQLite helpers are
reached as `from mailarc_core.database import sqlite`.

## Reading and writing the graph

```python
from runic.ogm import Field, Node

from mailarc_core.graph import session


class Message(Node, labels=["Message"]):
    id: str = Field(primary_key=True)
    subject: str


with session(config) as graph:
    graph.add(Message(id="m1", subject="Hello"))
```

The session commits on a clean exit, rolls back on an exception, and closes
the connection either way — a `runic.ogm.Session` tracks entities, not
sockets, so ownership has to stop somewhere and it stops in `client.py`.
Against a backend whose driver is a `TransactionalGraphDriver` (Bolt, AGE)
that is a real transaction; FalkorDB has none, so each statement is atomic on
its own.

Nothing in that example names a database. Point `GraphConfig.backend` at
`neo4j` and the same code runs — which is what the tests in
`tests/graph/test_client.py` demonstrate, driving `client` with a fake that
implements `GraphDriver` and imports no database client at all.

## Running FalkorDB on its own

```sh
uv run python -m mailarc_core     # or: task tauri:falkor
```

Settings come from the environment (`app_graph_*`) and `.env`; the mode is
forced to local.

## Rules

- **No Reflex, no `appkit_mantine`, no `appkit_user`.** The dependency list is
  the contract; a UI import here is a bug.
- The web application (`app/`) may import `mailarc_core`. Never the reverse.
