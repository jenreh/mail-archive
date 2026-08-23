# AGENTS.md

---

## 1) Golden Rules

**NEVER** guess — **ALWAYS** read the doc first (use Context7 or a skill)!

1. **Think → Memory → Tools → Code → Memory.** Use code-reasoning; search Memory and claude-context first; minimal diff; write learnings back.
2. **Tests are truth.** Failures → fix code first. Change tests only if clearly wrong spec.
3. **Minimal diff.** Add tests before code. Keep simple.
4. **Consistency > cleverness.** Follow SOPs and stack idioms.
5. **Memory multiplies.** Persist decisions, patterns, error signatures, proven fixes.
6. **Files ≤ 1000 lines.** Exceed → refactor (see §5).
7. No extensive docs/summaries/comments unless requested.
8. No `cat` to create files; use tools.
9. Log default: `logger.debug`. Important events: `logger.info`. Issues only: `logger.warning/error`. **No `print`.**
10. **Caveman skill** applies to all writes here.

> Prefer *local* changes over cross-module refactors.

---

## 2) Task Bootstrap Pattern

```markdown
<!-- plan:start
goal: <one line clear goal>
constraints:
- Python 3.14; FastAPI; Pydantic 2.13; pydantic-settings;
- logging: no f-strings in logger calls
- files ≤ 1000 lines; apply design patterns where appropriate
- minimal diff; add/adjust tests first
- hexagonal architecture: domain/application/ports/adapters/interfaces
definition_of_done:
- tests pass; coverage ≥ 80%; lint/type checks clean; memory updated
steps:
1) Search Memory for "<keywords>"
2) Draft/adjust failing test to capture expected behavior
3) Implement minimal code change
4) Run task test; iterate until green
5) Update Memory: decisions, patterns, error→fix
plan:end -->
```

---

## 3) Tooling Decision Matrix

| Situation | Primary | Secondary | Store to Memory |
| --- | --- | --- | --- |
| API/pattern uncertainty | **Context7** | — | Canonical snippet + link; edge cases |
| Ecosystem bug/issue | **DuckDuckGo** | Context7 | Minimal repro; versions; workaround |
| Repeated test failure | **Memory (search)** | Context7 | Error signature → fix; root cause |
| New feature scaffold | **Context7** | — | How‑to snippet; checklist |
| House style/tooling | **This file** | Context7 | Checklist results |

Prefer official docs; widen via web search for cross-version issues.

---

## 4) SOP — Development Workflow

**Task Runner:** `task` (via `Taskfile.dist.yml`), not `make`.

### Prepare

1. Memory first — search prior solutions.
2. Reasoning plan — Task Bootstrap Pattern.
3. `task test` — snapshot current failures.

### Triage Failures

- Read first failing assertion; map to spec.
- Tests match spec → fix code. Diverge → document; adjust spec/tests (after approval).
- Add/adjust unit tests to codify expected behavior.

### Implement (Minimal Diff)

- Tests-first for new behavior. Approved stacks only. Apply design patterns (see §5).
- **No `print`.** Use `logging` module.
- **No f-strings in logger calls:**

  ```python
  import logging

  log = logging.getLogger(__name__)
  log.info("Loaded items: %d", count)  # ✅
  # log.info(f"Loaded items: {count}") # ❌
  ```

### Quality Gates

- `task lint`, `task format`, `task typecheck`.
- `task test` — coverage ≥ **80%**.

### Commit & PR

- Conventional Commits (`feat:`, `fix:`, `refactor:`…).
- PR: description, `Closes #123`, screenshots, migration rationale.

### Learn → write to **Memory**

### Dependencies

- Add dependencies always via `uv add <library name>`

---

## 5) Python Code & Testing

Full rules in **python-coding** skill. Key:

- Python 3.14; uv; line length **88**.
- No f-strings in logger calls.
- Files ≤ 1000 lines.
- Coverage ≥ 80%.
- Type annotations on all functions/methods.
- **Value objects are pydantic models, never `@dataclass`** — immutable ones get
  `model_config = ConfigDict(frozen=True)`. This includes Reflex state var
  types: Reflex serialises `BaseModel` and resolves `row.field` inside
  `rx.foreach`.
- **Architecture**: UI on top of components (see §6). Introduce an abstraction
  when there is a second implementation, not in anticipation of one.

---

## 6) Architecture Layers

A uv workspace: a Reflex application on top of six first-party components, one
of which an installation may leave out.

```sh
mail-archive/
├── app/                       pages, routes, composition root, worker,
│   │                          MCP entry point
│   ├── composition.py         builds the components from configuration
│   ├── configuration.py       AppConfig (composes each component's config)
│   └── mcp_server.py          `mail-archive-mcp` — thin; nothing imports it
├── components/
│   ├── mailarc-core/          domain, mail source port, graph ground truth,
│   │                          SQLite, blob store — no browser, no provider
│   ├── mailarc-sync/          engine, job queue, worker loop, provider registry
│   ├── mailarc-analytics/     derived nodes, analysis queries, embeddings
│   ├── mailarc-google/        Gmail, behind the mail source port
│   ├── mailarc-mcp/           the six read-only MCP tools — OPTIONAL, see below
│   └── mailarc-ui/            Reflex states + components
├── scripts/                   build-time tooling (never runs on a user machine)
└── src-tauri/                 the macOS desktop shell
```

The hierarchy *is* the import table — read it as the layering:

| Module | may import | may **not** import |
| --- | --- | --- |
| `mailarc-core` | appkit-commons, runic.ogm, pydantic, sqlalchemy | Reflex, any provider |
| `mailarc-google` | `mailarc-core`, httpx, google-auth | `mailarc-sync`, Reflex |
| `mailarc-sync` | `mailarc-core` | `mailarc_google`, Reflex |
| `mailarc-analytics` | `mailarc-core` | `mailarc-sync`, Reflex |
| `mailarc-mcp` | `mailarc-core`, `-analytics`, fastmcp | `mailarc-sync`, `mailarc_google`, Reflex |
| `mailarc-ui` | `mailarc-core`, `-sync`, `-analytics`, reflex, appkit-mantine/-user | `app` |
| `app` | everything | — |

**No module imports `runic.rag`, `app` included.** Email already carries an
exact graph in its headers — senders, recipients, threads, labels — so an LLM
extraction would only lay a probabilistic layer over ground truth. A model
reads the archive at query time through the MCP server; it never writes to it.

Key rules:

- `app/` may import a component. A component **never** imports `app`.
- **`mailarc-ui` is the only component allowed to see Reflex** or an `appkit`
  UI package; every other one must stay usable from a CLI, a worker or a test.
  `components/mailarc-core/tests/test_isolation.py` enforces that exemption and
  the `runic.rag` ban from a subprocess.
- `app` is the only place that knows concrete implementations:
  `registry.register(GmailSource.DESCRIPTOR, …)` happens in
  `app/composition.py`, and the worker entrypoint is `app/worker.py` — else
  `mailarc-sync` would have to know the providers.
- Inside a component: one package per capability, with fixed file roles.
  `__init__.py` is the public surface, `model.py` holds value objects and knows
  no I/O, `config.py` the `BaseConfig`, `ports.py` only once a second
  implementation exists; every other module is one responsibility.
  `mailarc_core.graph` is the worked example.
- `app/composition.py` is the **only** module that builds a component from
  configuration. States and pages ask it; they never construct anything.
- A `Protocol` earns its place when a second implementation exists. One
  implementation behind a port is indirection, not architecture.
- **`mailarc-mcp` is optional and must stay optional.** It sits behind
  `[project.optional-dependencies] mcp`, so `uv sync` resolves the desktop
  bundle (82 distributions) and `uv sync --extra mcp` the web deployment (125)
  — `fastmcp` alone is around sixty and a desktop archive serves no MCP.
  `app/mcp_server.py` is the console script's entry point and the only module
  under `app/` allowed to name the component; **nothing may import that module,
  `mailarc_mcp` or `fastmcp` at import time**, or `app/app.py` and
  `app/worker.py` stop starting on exactly the installation the extra exists to
  produce. `tests/test_mcp_server.py` reads every module in `app/` and checks;
  `task tauri:deps` prints both resolutions. A developer environment is the web
  one — `task install` syncs `--extra mcp`.

---

## 6b) Never Touch the Real Archive

A developer machine holds one live archive and it is real mail: accounts and
encrypted credentials in `.state/mail-archive.db`, the original bytes of every
imported message in `.state/mailstore`, the graph in `.state/falkordb`. The blob
store is content-addressed and write-once, so anything written into it cannot
afterwards be told apart from a genuinely archived message.

Two mechanisms keep work away from it. Do not defeat either.

- **Tests** are sealed by the root `conftest.py`: it redirects every `app_*`
  setting into a temporary directory before collection and fails the run if
  `.state` changes while the suite is running. Never point a test at `.state`,
  and never construct a config that writes without passing an explicit path.
- **Driving the application** — a preview server, a rebuild, a worker, a browser
  check — goes through the **`agent:` task namespace**, never the real one:

  ```sh
  task agent:app                              # app on 8081/3031
  task agent:worker                           # worker against the same sandbox
  task agent:derive                           # rebuild the derived layer
  task agent:exec -- uv run python -c '...'   # anything else
  task agent:check                            # show what the sandbox resolves to
  task agent:clean                            # delete it
  ```

  **`PROFILES=agent_test` on its own is NOT enough**, and this is the trap:
  the profile YAML nests its settings under `app.archive`, `app.graph` and so
  on, which reach a component only through the composition root. A module that
  builds a bare config — `MessageArchiver(ArchiveConfig())`, which is what
  `planted_graph.py` does — gets its own settings source, finds nothing at the
  YAML top level, and falls back to the field default, which is the REAL store.
  Observed: `PROFILES=agent_test` → `ArchiveConfig().store_dir == .state/mailstore`.
  `taskfiles/Taskfile.agent.yml` exports the `app_*` variables that close that
  gap; use it rather than setting `PROFILES` by hand. `.state-agent/` is
  gitignored and disposable.

**UI test login.** Pages behind `@authenticated` are reached with the test
account `test@test.de` / `Test#2026`. Dev-only, seeded in the local SQLite;
not a production secret.

**`PROFILES` belongs in the entry point, never in `.env`.** `appkit_commons`
calls `load_dotenv(override=True)` at import, so a value in `.env` beats the
real process environment and pins every entry point to one profile — which is
how `PROFILES=prod task tauri:dev` silently ran the dev profile. Each task
exports its own.

**The database override env var is `app_database_url_override`, not
`app_database_url`.** `DatabaseConfig.url` is a computed field over a stored
`url_override`; the obvious name is accepted and silently ignored, and the
config falls through to `config.yaml` — the real archive.

---

## 7) Security & Config

- No credentials in code/history; `.env` local, SSM/Secrets Manager in prod.
- Non-secret YAML (`config/devices.yaml`); env `__` override pattern.
- Parameterized logs; no sensitive values.
- `SecretStr` → `.get_secret_value()`.
- HMAC Shared-Secret for AWS→home traffic (replay protection via timestamp).
- Update vulnerable deps; document CVE-driven updates in commits & Memory.

---

## 8) Search SOPs

- **Context7 first** for framework truths; cite in Memory.
- **DuckDuckGo** for cross-version issues; prefer official docs.
- Store only final answer: minimal snippet + rationale + version pins + link.

---

## 9) Task Checklist / Definition of Done

use **code-review** skill and ensure:

- [ ] Tests added/updated; all green
- [ ] Coverage ≥ 80%
- [ ] `task format && task lint && task typecheck` pass
- [ ] No file > 1000 lines
- [ ] Clean architecture, no code smells, used **python-clean-code** principles
- [ ] Migrations reviewed & documented
- [ ] Documentation & README.md updated
- [ ] Memory updated (decisions, patterns, error→fix, learnings)

---

## 10) Important Skills

Only the skills matching this project's configuration are listed below.

| Skill | Purpose |
| --- | --- |
| `python-coding` | Python 3.14 style, logging, type annotations, design patterns, testing |
| `python-clean-code` | Enforce Clean Code Developer (CCD) architecture and software quality principles |
| `code-cleanup` | Refactor and simplify Python files modified in the current session if they get complex/big |
| `boost` | Use when the user wants to refine, sharpen, or expand a rough idea into a detailed implementation prompt |
| `reflex-state-and-architecture` | State design, event handlers, background tasks, form validation, page factory, service registry, repo pattern, DB models, architecture |
| `reflex-testing-state` | Pytest unit tests for Reflex State — event handlers, computed vars, substates |
| `reflex-docs` | Reflex.dev framework documentation |
| `frontend-design` | Create distinctive, production-grade frontend interfaces |
| `appkit-mantine-reference` | Full API for appkit_mantine components — inputs, layout, overlays, charts, data display, navigation |
| `appkit-commons` | app configuration, service registry, DB repository pattern, DB entities, DB custom column types, scheduler |
| `docker-multi-stage` | Optimized multi-stage Dockerfiles, layer caching, security, healthchecks |
| `runic-migrate` | Author and apply graph schema migrations |
| `runic-ogm` | Object Graph Mapping for Cypher graph databases, query builder, sessions, relations |
