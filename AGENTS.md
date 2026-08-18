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
- **Architecture**: hexagonal (domain → application → ports ← adapters; interfaces deliver).

---

## 6) Architecture Layers

```
mail-archive/
├── ...
```

Key rules:

- Use-cases import `ports/` only (never `adapters/`).
- Adapters import `domain/` and their external library only.
- Composition root is the **only** place that knows both ports and adapters.

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
