# Changelog

All notable changes to this repo are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are git tags.

## [Unreleased]

## [0.3.0] - 2026-08-08

### Added

- Conformance with the vendor-neutral [Agent Plugins 1.0.0](https://agent-plugins.org)
  spec. The spec paths it requires at each plugin root are `ln -s` symlinks onto the
  existing Claude files, so there is still exactly one file per plugin to maintain:
  `plugins/<p>/plugin.json -> .claude-plugin/plugin.json` and
  `plugins/<p>/mcp.json -> .mcp.json`. The spec permits this — symlinks may resolve to
  targets inside the plugin root — and nothing is duplicated.
- `scripts/validate_plugins.py`, an offline mirror of the two published schemas, wired
  into `task validate` / `task validate:plugins` and the CI workflow. Beyond the schemas
  it enforces that each spec path and its Claude counterpart are the same inode, so the
  two views cannot drift.

### Changed

- Bumped all four marketplace plugins from `0.1.0` to `0.3.0` to match this repo's
  release tag — their versions had never moved despite the skill churn in 0.2.0
  (skills added, removed, and `appkit-mantine-reference` expanded). Also fixed
  `jenreh-core` and `jenreh-python` descriptions in `marketplace.json` and
  `scripts/link-plugins.sh`, which still named `skills-creator`, `skills-find`, and
  `runic` after they were dropped.
- Extended the Claude manifests with the spec metadata that does not collide with them:
  `plugin.json` gained `$schema`, `homepage`, `repository` and `keywords`; `.mcp.json`
  gained the required `$schema` and an explicit `"type": "stdio"` per server (Claude Code
  already accepted `type`). `claude plugin validate` still passes on all four plugins.
  `jenreh-python` / `jenreh-reflex` keep Claude Code's top-level `dependencies`, which the
  spec reports and ignores rather than rejecting — moving it under `extensions` would
  break Claude Code's dependency auto-install.
- `scripts/link-plugins.sh` now generates the full manifest (it had drifted from the
  hand-edited `repository` / `dependencies` fields and would have clobbered them) and
  creates the spec symlinks.
- Documented that the `plugins/` marketplace layer (`.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json`) already works for GitHub Copilot CLI and VS Code's
  agent plugins, not just Claude Code — both check `.claude-plugin/` as a fallback
  manifest location, so no dual manifest is needed. Added `copilot plugin ...` install
  instructions to the README alongside the existing `/plugin ...` Claude Code commands,
  and a maintainer note in `docs/cross-agent-skills.md` about keeping it that way.

### Fixed

- `reflex-testing-state/SKILL.md` frontmatter: an unindented continuation line ended the
  folded `description:` block early, so the YAML failed to parse and the skill loaded with
  all metadata silently dropped.
- `.claude-plugin/marketplace.json`: plugin `source` values now use the `./<name>` form
  the marketplace schema requires; all four entries previously failed validation.

## [0.2.0] - 2026-07-02

### Changed

- Inverted the skill symlink direction so marketplace plugins are self-contained:
  each skill's real files now live under `plugins/jenreh-{core,python,reflex,terraform}/skills/`,
  and the top-level `skills/` entries are symlinks pointing into the plugins. This
  lets plugins be vendored independently without dangling references.
- Expanded the `appkit-mantine-reference` skill to Mantine 9.4: split the monolithic
  `inputs.md` into focused references (text, selection, toggle, datetime, specialized)
  and added coverage for charts, data display, layout, overlays, menus, navigation,
  tables, trees, typography, theming, feedback, extensions, and scheduling.

### Removed

- Dropped `runic`, `skills-creator`, and `skills-find` from `skills/` — these are
  standard skills already available through plugin marketplaces, so vendoring them
  here was redundant.

## [0.1.0] - 2026-06-13

### Added

- Initial canonical, portable agent-skills repo: single source of truth for skills
  shared across Claude Code, Codex, and GitHub Copilot.
- 27 skills migrated from `jenreh/project-kit` (a strict superset of `python-kit`),
  all verified portable-core (no Claude-only / Codex-only frontmatter).
- `scripts/add-skills.sh` — one-command vendor + cross-agent wiring (`git subtree`
  into `.agents/agent-skills` + `.agents/skills` / `.claude/skills` symlinks).
- `scripts/validate_skills.py` + `task validate` + GitHub Actions `validate` workflow.
- `docs/cross-agent-skills.md` documenting the D3/D4 conventions.
- Optional Claude Code marketplace layer (`.claude-plugin/marketplace.json`,
  `plugins/jenreh-{core,python,reflex,terraform}`) with skills symlinked back to
  `skills/` (no duplication).
