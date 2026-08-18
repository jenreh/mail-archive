# agent-skills

Canonical, portable [agent skills](https://agentskills.io) shared across **Claude Code**,
**Codex**, and **GitHub Copilot** — a single source of truth, vendored into projects via
`git subtree` and wired so every agent resolves the same physical files.

Each skill is a `skills/<name>/SKILL.md` folder using only the portable core (name +
description + markdown, with optional `scripts/`, `references/`, `assets/`). No Claude-only
or Codex-only frontmatter, so the bytes are identical for every agent.

## What's here

```
skills/<name>/SKILL.md   # 25 portable skills
scripts/
  add-skills.sh          # vendor + wire into any repo (one command)
  validate_skills.py      # portable-core validator (run in CI)
  validate_plugins.py     # Agent Plugins 1.0.0 conformance validator
Taskfile.yml             # task validate | task list
docs/cross-agent-skills.md
.claude-plugin/, plugins/  # optional marketplace / Agent Plugins layer
```

Run `task list` to see every skill and its trigger description, or `task validate` to check
portability.

## Consume it

### Any repo (all three agents) — recommended

```bash
curl -fsSL https://raw.githubusercontent.com/jenreh/agent-skills/main/scripts/add-skills.sh | bash
# or, if you've already cloned this repo:
bash scripts/add-skills.sh
```

This vendors the repo via `git subtree` into `.agents/agent-skills/` and creates two
symlinks so each agent finds the skills from one copy:

```
.agents/skills  -> agent-skills/skills              # Codex + Copilot
.claude/skills  -> ../.agents/agent-skills/skills   # Claude Code
```

Update later:

```bash
git subtree pull --prefix .agents/agent-skills https://github.com/jenreh/agent-skills.git main --squash
```

See [docs/cross-agent-skills.md](docs/cross-agent-skills.md) for the full convention.

### Per-agent native paths

- **Claude Code** — `/plugin marketplace add gh:jenreh/agent-skills` then `/plugin install`
  (see the [plugin marketplace](#plugin-marketplace) below), or just run `add-skills.sh`.
- **Codex** — run `add-skills.sh`; Codex scans `.agents/skills` and follows the symlink.
- **GitHub Copilot** — `copilot plugin marketplace add gh:jenreh/agent-skills` then
  `copilot plugin install` (same marketplace layer, see below), or run `add-skills.sh`, or
  drop skills into `.github/skills`.

### Copier templates

The [python-kit-template](https://github.com/jenreh/python-kit-template) and
[project-kit-template](https://github.com/jenreh/project-kit-template) Copier templates
wire these skills automatically when you answer `include_skills: true`.

## Plugin marketplace

For ad-hoc projects, skills are also grouped into installable plugins under `plugins/` —
usable from both **Claude Code** and **GitHub Copilot** (CLI + VS Code).
The manifests (`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`) use the
format shared by Claude Code, the GitHub Copilot CLI, and VS Code's agent plugins — both
tools check `.claude-plugin/` as a fallback manifest location, so **one set of files
serves both ecosystems** with no dual manifests needed.

The same plugins also conform to the vendor-neutral
[Agent Plugins 1.0.0](https://agent-plugins.org) spec, which expects `plugin.json` and
`mcp.json` at the plugin root. Those spec paths are `ln -s` symlinks onto the Claude
files — one file per plugin, no copies, no drift:

```
plugins/jenreh-core/plugin.json -> .claude-plugin/plugin.json
plugins/jenreh-core/mcp.json    -> .mcp.json
```

See [docs/cross-agent-skills.md](docs/cross-agent-skills.md#agent-plugins-100-conformance)
for what each file carries and the one accepted deviation.

**Claude Code:**

```text
/plugin marketplace add gh:jenreh/agent-skills
/plugin install jenreh-core@jenreh        # boost, commit-msg, create-readme, …
/plugin install jenreh-python@jenreh      # python-coding, runic, docker, …
/plugin install jenreh-reflex@jenreh      # appkit + reflex skills
/plugin install jenreh-terraform@jenreh   # terraform-*
```

**GitHub Copilot CLI** (and picked up automatically by VS Code's Agent Plugins view):

```text
copilot plugin marketplace add jenreh/agent-skills
copilot plugin install jenreh-core@jenreh
copilot plugin install jenreh-python@jenreh
copilot plugin install jenreh-reflex@jenreh
copilot plugin install jenreh-terraform@jenreh
```

**VS Code** also discovers plugins registered as a marketplace via the
`chat.plugins.marketplaces` setting, or installed locally with `chat.pluginLocations`.

> `jenreh-python` registers a Python LSP server (`pyright-langserver`) via `.lsp.json`.
> Install it manually with `pip install pyright` — the plugin does not install it for you.

Plugin updates are refresh + reinstall (`/plugin marketplace update` or
`copilot plugin update --all`, then reinstall); plugin skills are namespaced, e.g.
`jenreh-core:boost`. Each plugin's `skills/` folder holds real files (not symlinks) so the
plugin directory is self-contained and can be vendored independently; the top-level
`skills/` folder symlinks back into the plugins for the portable/subtree consumption path
above.

## Develop

```bash
task validate           # portable core + Agent Plugins conformance (runs in CI)
task validate:skills    # portable core only
task validate:plugins   # Agent Plugins 1.0.0 only
task list               # list skills + descriptions
```

The bundled validator is self-contained. The hosted validators at
[agentskills.io](https://agentskills.io) / skills.sh are stricter on some optional fields —
run `npx skills validate` if you want the full check.

## License

MIT — see [LICENSE](LICENSE). Individual skills may carry their own license file
(e.g. `frontend-design/LICENSE.txt`); those take precedence for that skill.
