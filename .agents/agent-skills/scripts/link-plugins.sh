#!/usr/bin/env bash
# (Re)build the repo's two layouts from a single source of truth.
#
# Source of truth: the REAL skill folders live under each plugin:
#     plugins/<plugin>/skills/<name>/SKILL.md
# This is what a marketplace install copies standalone (a plugin folder is
# copied on its own, so its skills must be real files — never symlinks that
# escape it). The generated plugins/<plugin>/.claude-plugin/plugin.json is
# read by Claude Code, the GitHub Copilot CLI, and VS Code alike — all three
# check .claude-plugin/ as a fallback manifest location, so this one manifest
# serves every tool (see docs/cross-agent-skills.md). Do not add a second,
# Copilot-specific manifest — it would just duplicate this and drift.
#
# Agent Plugins 1.0.0 (https://agent-plugins.org) puts the manifest at
# `<plugin>/plugin.json` and the MCP config at `<plugin>/mcp.json`. Rather than
# maintaining two copies, the spec paths are symlinks into the Claude/Copilot
# locations — the spec allows this as long as they resolve inside the plugin
# root, which relative links to `.claude-plugin/plugin.json` and `.mcp.json` do.
#
# This script:
#   * writes each plugin's .claude-plugin/plugin.json
#   * links <plugin>/plugin.json -> .claude-plugin/plugin.json  (Agent Plugins)
#   * links <plugin>/mcp.json    -> .mcp.json                   (Agent Plugins)
#   * regenerates the flat, portable view used by Codex/Copilot:
#       skills/<name> -> ../plugins/<plugin>/skills/<name>   (read in place)
# Skills are NEVER duplicated — the flat skills/ dir points back at the plugins.
set -euo pipefail
cd "$(dirname "$0")/.."

declare -a PLUGINS=(jenreh-core jenreh-python jenreh-reflex jenreh-terraform)

PLUGIN_SCHEMA="https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
REPO_URL="https://github.com/jenreh/agent-skills"
VERSION="0.3.0"

desc_jenreh_core="Cross-cutting agent skills (boost, commit-msg, create-readme, release, …)."
desc_jenreh_python="Python archetype skills (python-coding, runic-ogm, docker-multi-stage, …)."
desc_jenreh_reflex="reflex.dev / AppKit web skills (appkit-*, reflex-*)."
desc_jenreh_terraform="Terraform / infra skills (terraform-*)."

# JSON array bodies (without the brackets). Empty = omit the field.
keywords_jenreh_core='"agent-skills", "claude-code", "git", "release", "documentation"'
keywords_jenreh_python='"agent-skills", "claude-code", "python", "docker", "ci-cd"'
keywords_jenreh_reflex='"agent-skills", "claude-code", "reflex", "python", "frontend"'
keywords_jenreh_terraform='"agent-skills", "claude-code", "terraform", "azure", "iac"'

# `dependencies` is a Claude Code field. Agent Plugins reports it as an unknown
# top-level field and ignores it, which is a warning — not a rejection.
deps_jenreh_core=''
deps_jenreh_python='{ "name": "jenreh-core" }'
deps_jenreh_reflex='{ "name": "jenreh-core" }, { "name": "jenreh-python" }'
deps_jenreh_terraform=''

# 1) Write plugin.json for each plugin, then expose it at the spec path.
for plugin in "${PLUGINS[@]}"; do
  pdir="plugins/$plugin"
  mkdir -p "$pdir/.claude-plugin" "$pdir/skills"
  key="${plugin//-/_}"
  descvar="desc_$key"
  kwvar="keywords_$key"
  depvar="deps_$key"

  {
    printf '{\n'
    printf '  "$schema": "%s",\n' "$PLUGIN_SCHEMA"
    printf '  "name": "%s",\n' "$plugin"
    printf '  "description": "%s",\n' "${!descvar}"
    printf '  "version": "%s",\n' "$VERSION"
    printf '  "author": { "name": "Jens Rehpöhler" },\n'
    printf '  "homepage": "%s",\n' "$REPO_URL"
    printf '  "repository": "%s",\n' "$REPO_URL"
    printf '  "license": "MIT",\n'
    printf '  "keywords": [%s]' "${!kwvar}"
    if [ -n "${!depvar}" ]; then
      printf ',\n  "dependencies": [%s]' "${!depvar}"
    fi
    printf '\n}\n'
  } > "$pdir/.claude-plugin/plugin.json"

  ln -sfn .claude-plugin/plugin.json "$pdir/plugin.json"

  # mcp.json is optional; only link it where a Claude .mcp.json actually exists.
  if [ -f "$pdir/.mcp.json" ]; then
    ln -sfn .mcp.json "$pdir/mcp.json"
  else
    rm -f "$pdir/mcp.json"
  fi
done

# 2) Rebuild the flat skills/ view from the real skill folders in the plugins.
mkdir -p skills
find skills -maxdepth 1 -type l -delete
count=0
for plugin in "${PLUGINS[@]}"; do
  for sdir in "plugins/$plugin/skills"/*/; do
    [ -d "$sdir" ] || continue
    skill=$(basename "$sdir")
    if [ -e "skills/$skill" ] && [ ! -L "skills/$skill" ]; then
      echo "ERROR: skills/$skill exists as a real path (expected a generated symlink)" >&2
      exit 1
    fi
    ln -sfn "../plugins/$plugin/skills/$skill" "skills/$skill"
    count=$((count + 1))
  done
  n=$(find "plugins/$plugin/skills" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
  echo "plugin $plugin: $n skills"
done
echo "linked $count skills into the flat skills/ view"
