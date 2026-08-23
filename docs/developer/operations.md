# Operations

Every command in this project is a `task`. Not `make`.

```sh
task            # list them all
```

## Two migration systems

The relational store and the graph each have their own, and neither knows about
the other.

| | Relational | Graph |
| --- | --- | --- |
| Tool | Alembic | `runic.migrate` |
| Namespace | `task db:*` | `task graph:*` |
| Versions | `alembic/versions/` | `graph_migrations/versions/` |
| Config | `alembic.ini`, `alembic/env.py` | `graph_migrations/env.py` |

### Relational

```sh
task db:upgrade                    # to head
task db:current                    # where are we
task db:history
task db:revision -- "add a column"
task db:downgrade                  # back one
```

**Write migrations by hand.** Autogenerate is an anti-pattern here, and it would
happily drop the tables `appkit_user` owns. The mail-import migration says so in
its own docstring.

`alembic/env.py` imports `mailarc_core.database.entities` purely for the side
effect — a table is only in `Base.metadata` once its module has run — and asks
`sync_database_url()` for the blocking URL, because the configured one names
`aiosqlite`.

Revision chain: `appkit_user` → `mail_import`.

### Graph

```sh
task graph:upgrade                 # to head
task graph:current
task graph:history
task graph:revision -- "add an index"
task graph:downgrade
```

Two things about the scaffold that will otherwise cost you an afternoon:

- It lives in `graph_migrations/`, **not** runic's default `runic/`. A package
  directory of that name at the repository root shadows the installed `runic`
  package on `sys.path`. Every command therefore needs
  `--config graph_migrations/env.py`, which `task graph:*` passes for you.
- `graph_migrations/env.py` reads host, port and graph name from `GraphConfig`,
  not from a second set of environment variables. So a migration can never be
  applied to a different graph than the one the app reads — `APP_GRAPH_HOST` and
  friends move both.

Only FalkorDB is wired up there. It is the backend this project ships and the
only one it can start locally.

Revision chain: `fc2f7a8d4b66` (the ground truth's indexes) → `53fcf8d0fe56`
(the three the derived layer's primary keys need, plus `Message.id`, which the
derived reader pages the archive by).

#### Two FalkorDB quirks the baseline records

- `create_constraint` creates its own index unconditionally, and FalkorDB
  rejects a second `CREATE INDEX` on an already-indexed attribute. So no
  explicit range index on `rfc_message_id` — asking for both fails the
  migration. Verified against a live server.
- One full-text index covers **both** `subject` and `body_text`. FalkorDB keeps
  one such index per label, and the archive searches them together anyway.

Drop order in `downgrade` is the reverse: constraint before its index, or the
drop fails.

#### The derived layer

```sh
task graph:rebuild-derived         # delete Group/Topic/Template, compute again
```

Not a migration, and deliberately in the same namespace as one so that the two
things that write graph structure are found in the same place. It runs
`python -m app.derive`, which opens one session on the configured graph and asks
`mailarc-analytics` to delete every `Group`, `Topic` and `Template` node and
every `CO_ADDRESSED` edge, then recompute all three from the ground truth. Safe
to run at any time: derived nodes are disposable by construction, and a second
run over an unchanged archive writes exactly the same graph.

The same work is available as a queued job — `kind=derive`, with no account,
because it is about the whole archive — so a long rebuild reports into a row
that can be read and cancelled while it runs, instead of holding a terminal.
**Rebuild** on `/admin/insights` enqueues that job and follows the row; the task
above is the same rebuild with no queue and no worker in the way, which is what
makes it the one to reach for when the worker is what you are debugging.

## Running things

```sh
PROFILES=local task run       # web app, frontend :8080 + backend :3030
task run:debug                # same, with debug logging
task run:prod                 # reflex run --env prod --single-port
task sync:worker              # the import worker, foreground
```

## The quality gate

```sh
task format && task lint && task typecheck && task test
```

| Task | Runs |
| --- | --- |
| `format` | `ruff check --fix .` then `ruff format .` |
| `lint` | `ruff check .` |
| `typecheck` | `ty check app components/*/src` |
| `test` | `pytest --cov`, failing under 80 % |

Every phase of this project ends with all four green. See
[testing](testing.md).

## Adding a dependency

```sh
uv add <package>                                  # the application
uv add --package mailarc-core <package>           # one component
uv add --group dev <package>                      # tooling
```

Never hand-edit `pyproject.toml` for a dependency — `uv add` keeps `uv.lock`
honest.

`uv sync` installs the root's dependency closure and nothing else, so a
workspace member nothing depends on yet is not importable. Five of the six are
in that closure: `mailarc-analytics` moved out of the `dev` group and into
`[project] dependencies` when the `derive` job and `task graph:rebuild-derived`
gave the application a reason to import it.

`mailarc-mcp` is deliberately outside it, behind `[project.optional-dependencies]
mcp`. `uv sync` is the desktop bundle's shape (82 distributions) and
`uv sync --extra mcp` the web deployment's (125) — `fastmcp` alone is around
sixty, and a desktop archive serves no MCP. `task install` and `task sync` ask
for the extra, because a developer environment is the web one and the test suite
covers the component; `task tauri:deps` prints both resolutions and fails if
`fastmcp` ever leaks into the first.

## Docker

```sh
task docker:build
task docker:run
```

`docker-compose.yml` and a multi-stage `Dockerfile` are in the repository root.
Under Docker, set `sync.supervise_worker: false` and run the worker as its own
service — otherwise the application starts a second one and the two race for the
same jobs.

## Syncing on a schedule

```yaml
app:
  sync:
    incremental_interval: 900   # seconds; 0 (the default) is off
```

Off by default: a fresh install must not start talking to somebody's mailbox on
its own. Turned on, the worker queues one `incremental` job per enabled mailbox
every interval — accounts waiting for a re-consent and accounts that already
have a sync going are skipped, and a delta that brought new mail in queues a
rebuild of the derived layer after itself. It only ever *enqueues*; the same
worker loop then runs those jobs like any other. See
[jobs and the worker](jobs-and-worker.md#the-recurring-trigger).

## The desktop app

See [the desktop app](../user/desktop-app.md). Briefly:

```sh
task tauri:init
task tauri:vendor
task tauri:vendor:verify
task tauri:build
```

## Backup and restore

Everything lives in one directory:

```text
.state/
├── mail-archive.db      accounts, credentials, jobs, checkpoints
├── mailstore/           the original bytes — this is the archive
└── falkordb/            the graph's data
```

Stop the application first, so SQLite's WAL and FalkorDB's snapshot are both at
rest, then copy `.state/`.

What is genuinely irreplaceable is `mailstore/`. The graph can be rebuilt from
those bytes; the relational ledger can be rebuilt from the graph. Credentials
cannot be rebuilt from anything — but they can be re-granted, and re-granting is
cheaper than restoring a leaked one.

`task clean` removes `.state/` entirely, along with `.web/`, `dist/`, `build/`
and the caches. It destroys the archive. `task db:upgrade` afterwards recreates
the schema and the default `admin` account, and nothing else.

## Logging

`logging.yaml` for development, `logging.prod.yaml` for production, selected by
`app.logging` in the active profile.

House rules, neither of which ruff enforces:

```python
log.info("Loaded items: %d", count)  # ✅ parameterised
log.info(f"Loaded items: {count}")  # ❌ f-string in a logger call
print(count)  # ❌ never
```

Default level is `debug` for ordinary flow, `info` for events worth a line in
production, `warning`/`error` for actual problems. And never log a secret —
`SecretStr` stays wrapped until `.get_secret_value()` at the point of use.

## Health

The home page reports the graph server: reachable or not, latency, Redis and
FalkorDB versions, server metrics, the graphs behind the endpoint, and whether
the version supports the vector KNN queries later phases need.

An unreachable server is a **status**, not an error — the reader returns
`reachable=false` with a reason rather than raising, so the panel renders "down"
like any other state.

If the graph server failed to start, `graph_startup_error()` carries why, and
the page shows it. The application deliberately stays up: the page whose whole
job is reporting server state is more useful up than down.
