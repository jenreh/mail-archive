#!/usr/bin/env bash
# Vendor jenreh/agent-skills into any git repo and wire it for Claude Code,
# Codex, and GitHub Copilot from a single physical copy.
#
# Layout produced (skills live under skills/ in the canonical repo, so we vendor
# the whole repo into .agents/agent-skills and point two symlinks at it):
#
#   .agents/agent-skills/   <- git subtree (the whole agent-skills repo)
#   .agents/skills          -> agent-skills/skills        (Codex / Copilot read here)
#   .claude/skills          -> ../.agents/agent-skills/skills  (Claude Code reads here)
#
# Both symlinks are single-hop and point at the real directory — no chains.
set -euo pipefail

REPO="${AGENT_SKILLS_REPO:-https://github.com/jenreh/agent-skills.git}"
BRANCH="${AGENT_SKILLS_BRANCH:-main}"
PREFIX=".agents/agent-skills"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: run this inside a git repository" >&2
  exit 1
fi

if [ -d "$PREFIX" ]; then
  echo "agent-skills already vendored at $PREFIX."
  echo "Update it with:"
  echo "  git subtree pull --prefix $PREFIX $REPO $BRANCH --squash"
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is dirty; commit or stash first (git subtree needs a clean tree)" >&2
  exit 1
fi

echo "Vendoring agent-skills via git subtree -> $PREFIX ..."
git subtree add --prefix "$PREFIX" "$REPO" "$BRANCH" --squash

mkdir -p .agents .claude
ln -sfn agent-skills/skills .agents/skills
ln -sfn ../.agents/agent-skills/skills .claude/skills

git add .agents/skills .claude/skills
git commit -m "chore: wire .agents/skills + .claude/skills -> agent-skills"

cat <<EOF

Done. Skills are now discoverable by Claude Code (.claude/skills),
Codex and Copilot (.agents/skills).

Update later with:
  git subtree pull --prefix $PREFIX $REPO $BRANCH --squash
EOF
