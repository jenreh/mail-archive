#!/usr/bin/env python3
"""Validate plugins/ against the Agent Plugins 1.0.0 specification.

https://agent-plugins.org — Working Draft. Self-contained (stdlib only), so it
runs the same on a laptop and in CI without a network round-trip. It mirrors the
two published schemas rather than fetching them:

  * https://agent-plugins.org/schemas/1.0.0/plugin.schema.json
  * https://agent-plugins.org/schemas/1.0.0/mcp.schema.json

Layout note: the spec paths `<plugin>/plugin.json` and `<plugin>/mcp.json` are
symlinks into the Claude/Copilot locations (`.claude-plugin/plugin.json` and
`.mcp.json`) so there is exactly one file per plugin to maintain. The spec allows
that — symlinks may resolve to targets inside the plugin root — and this script
enforces the containment rule plus the "no drift" invariant that the two paths
really are the same inode.

Error vs warning follows the spec's own failure boundaries: an unknown top-level
manifest field is reported and ignored (warning), while anything that would make
a client reject the plugin or skip a component is an error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
CWD_RE = re.compile(r"^(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$))")
NAMESPACE_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")

MANIFEST_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
AUTHOR_FIELDS = {"name", "email", "url"}

# Fields we knowingly carry outside the portable core. Conformant clients report
# and ignore them; we surface them as one informational warning instead of noise.
KNOWN_CLIENT_FIELDS = {"dependencies": "Claude Code"}

SERVER_FIELDS = {
    "stdio": ({"type", "command"}, {"type", "command", "args", "env", "cwd"}),
    "streamable-http": ({"type", "url"}, {"type", "url", "headers"}),
    "sse": ({"type", "url"}, {"type", "url", "headers"}),
}

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def load_json(path: Path, label: str, rep: Report) -> dict | None:
    """Read a JSON object, reporting parse/type/containment failures."""
    root = path.parent.resolve()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        rep.error(f"{label}: cannot resolve ({exc})")
        return None
    # Spec: a package path must resolve inside the plugin root. Symlinks are fine
    # as long as their target stays in.
    if root not in resolved.parents and resolved != root:
        rep.error(f"{label}: resolves outside the plugin root ({resolved})")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        rep.error(f"{label}: {exc}")
        return None
    if not isinstance(data, dict):
        rep.error(f"{label}: top level must be an object")
        return None
    return data


def check_manifest(plugin: Path, rep: Report) -> None:
    label = f"{plugin.name}/plugin.json"
    path = plugin / "plugin.json"
    if not path.is_file():
        rep.error(f"{label}: missing (spec requires the manifest at the plugin root)")
        return

    claude_path = plugin / ".claude-plugin" / "plugin.json"
    if claude_path.is_file() and path.resolve() != claude_path.resolve():
        rep.error(f"{label}: not the same file as .claude-plugin/plugin.json (will drift)")

    data = load_json(path, label, rep)
    if data is None:
        return

    if data.get("$schema") != PLUGIN_SCHEMA:
        rep.error(f"{label}: $schema must be '{PLUGIN_SCHEMA}'")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        rep.error(f"{label}: 'name' is required")
    else:
        if not (1 <= len(name) <= 64) or not NAME_RE.match(name):
            rep.error(f"{label}: name '{name}' violates the spec pattern")
        if name != plugin.name:
            rep.error(f"{label}: name '{name}' != directory name '{plugin.name}'")

    for field in ("version", "description", "homepage", "repository", "license"):
        if field in data and not isinstance(data[field], str):
            rep.error(f"{label}: '{field}' must be a string")

    keywords = data.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords)
    ):
        rep.error(f"{label}: 'keywords' must be an array of strings")

    author = data.get("author")
    if author is not None:
        if not isinstance(author, dict):
            rep.error(f"{label}: 'author' must be an object")
        else:
            for key in set(author) - AUTHOR_FIELDS:
                rep.error(f"{label}: author.{key} is not allowed by the spec")

    extensions = data.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, dict):
            rep.error(f"{label}: 'extensions' must be an object")
        else:
            for ns, value in extensions.items():
                if not NAMESPACE_RE.match(ns):
                    rep.error(f"{label}: extension key '{ns}' is not a reverse-domain namespace")
                if not isinstance(value, dict):
                    rep.error(f"{label}: extensions.{ns} must be an object")

    for field in sorted(set(data) - MANIFEST_FIELDS):
        owner = KNOWN_CLIENT_FIELDS.get(field)
        if owner:
            rep.warn(f"{label}: '{field}' is a {owner} field; conformant clients ignore it")
        else:
            rep.warn(f"{label}: unknown top-level field '{field}'")


def check_server(label: str, name: str, server: object, rep: Report) -> None:
    if not isinstance(server, dict):
        rep.error(f"{label}: server '{name}' must be an object")
        return

    transport = server.get("type")
    if transport not in SERVER_FIELDS:
        rep.error(
            f"{label}: server '{name}' has type {transport!r}; "
            f"expected one of {', '.join(sorted(SERVER_FIELDS))}"
        )
        return

    required, allowed = SERVER_FIELDS[transport]
    for field in sorted(required - set(server)):
        rep.error(f"{label}: server '{name}' is missing required '{field}'")
    for field in sorted(set(server) - allowed):
        rep.error(f"{label}: server '{name}' has '{field}', not allowed for {transport}")

    for field in ("command", "url"):
        if field in server and not (isinstance(server[field], str) and server[field]):
            rep.error(f"{label}: server '{name}' has an empty '{field}'")

    args = server.get("args")
    if args is not None and (
        not isinstance(args, list) or not all(isinstance(a, str) for a in args)
    ):
        rep.error(f"{label}: server '{name}' args must be an array of strings")

    env = server.get("env")
    if env is not None:
        if not isinstance(env, dict):
            rep.error(f"{label}: server '{name}' env must be an object")
        else:
            for key, value in env.items():
                if key in ("PLUGIN_ROOT", "PLUGIN_DATA"):
                    rep.error(f"{label}: server '{name}' must not redefine env '{key}'")
                if not isinstance(value, str):
                    rep.error(f"{label}: server '{name}' env.{key} must be a string")

    cwd = server.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not CWD_RE.match(cwd)):
        rep.error(
            f"{label}: server '{name}' cwd must start with './', "
            "'${PLUGIN_ROOT}' or '${PLUGIN_DATA}'"
        )

    headers = server.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            rep.error(f"{label}: server '{name}' headers must be an object")
        elif not all(isinstance(v, str) for v in headers.values()):
            rep.error(f"{label}: server '{name}' header values must be strings")


def check_mcp(plugin: Path, rep: Report) -> None:
    label = f"{plugin.name}/mcp.json"
    path = plugin / "mcp.json"
    claude_path = plugin / ".mcp.json"

    if not path.exists():
        if claude_path.is_file():
            rep.error(f"{plugin.name}: has .mcp.json but no mcp.json (run scripts/link-plugins.sh)")
        return  # MCP configuration is optional.

    if claude_path.is_file() and path.resolve() != claude_path.resolve():
        rep.error(f"{label}: not the same file as .mcp.json (will drift)")

    data = load_json(path, label, rep)
    if data is None:
        return

    if data.get("$schema") != MCP_SCHEMA:
        rep.error(f"{label}: $schema must be '{MCP_SCHEMA}'")
    for field in sorted(set(data) - {"$schema", "mcpServers"}):
        rep.error(f"{label}: '{field}' is not allowed by the spec")

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        rep.error(f"{label}: 'mcpServers' is required and must be an object")
        return
    for name, server in servers.items():
        check_server(label, name, server, rep)


def check_skills(plugin: Path, rep: Report) -> int:
    skills = plugin / "skills"
    if not skills.exists():
        return 0  # A plugin without skills is still valid.
    if not skills.is_dir():
        rep.error(f"{plugin.name}/skills: exists but is not a directory")
        return 0

    count = 0
    for child in sorted(skills.iterdir()):
        if not child.is_dir():
            continue
        if (child / "SKILL.md").is_file():
            count += 1
        else:
            rep.error(f"{plugin.name}/skills/{child.name}: no SKILL.md")
    return count


def main(argv: list[str]) -> int:
    # Optional path argument so the checks can be exercised against a fixture.
    plugins_dir = Path(argv[0]).resolve() if argv else PLUGINS_DIR
    if not plugins_dir.is_dir():
        print(f"ERROR: {plugins_dir} not found", file=sys.stderr)
        return 2

    plugins = sorted(p for p in plugins_dir.iterdir() if p.is_dir())
    if not plugins:
        print("ERROR: no plugins found", file=sys.stderr)
        return 2

    rep = Report()
    total_skills = 0
    for plugin in plugins:
        check_manifest(plugin, rep)
        check_mcp(plugin, rep)
        total_skills += check_skills(plugin, rep)

    for w in rep.warnings:
        print(f"WARN  {w}")
    for e in rep.errors:
        print(f"ERROR {e}")

    print(
        f"\nValidated {len(plugins)} plugins ({total_skills} skills) against "
        f"Agent Plugins 1.0.0: {len(rep.errors)} error(s), {len(rep.warnings)} warning(s)."
    )
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
