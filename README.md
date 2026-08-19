<div align="center">

# mail-archive

**Email archive and analysis**

![Version](https://img.shields.io/badge/version-1.0.0-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE.md)
[![Python](https://img.shields.io/badge/python-3.14%2B-orange)](https://www.python.org)
[![Reflex](https://img.shields.io/badge/reflex-0.9.5-purple)](https://reflex.dev)

[Getting Started](#project-initialization) • [Branching](#branching)

</div>

---

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
app/                            the Reflex web application — pages, states, styles
  composition.py                the only place that builds the core from configuration
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
scripts/                        build-time tooling (vendoring FalkorDB, icons)
src-tauri/                      the macOS desktop shell
```

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

The relational store is a single SQLite file at `.state/mail-archive.db` — no
database server to start. `task clean` wipes `.state/`, so it takes the user
accounts with it; the first `task db:upgrade` after that recreates them,
including the default `admin` account.

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
FalkorDB, so **the built `.app` needs no Homebrew, no Docker and no Redis on the
target Mac**.

```sh
task tauri:init     # check the toolchain, install tauri-cli, generate icons
task tauri:vendor   # download + build the self-contained FalkorDB runtime
task tauri:dev      # run the desktop app
task tauri:build    # produce src-tauri/target/release/bundle/macos/mail-archive.app
```

Build-machine only requirements: Xcode command line tools, Rust, Node, and
Homebrew `openssl@3` (its dylibs get copied into the bundle and repointed at
`@loader_path`).

### What `task tauri:vendor` produces

| File | Where it comes from |
| --- | --- |
| `redis-server` | built from the pinned Redis source with `BUILD_TLS=no`, so it links only system libraries |
| `falkordb.so` | the pinned official FalkorDB release asset, SHA256-verified |
| `libssl.3.dylib`, `libcrypto.3.dylib` | copied from Homebrew, because the FalkorDB module links them at absolute paths |

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
