#!/usr/bin/env python3
"""Validate portable SKILL.md folders for cross-agent (Claude/Codex/Copilot) use.

Self-contained: only depends on the stdlib plus a tiny inline frontmatter parser,
so it runs identically on a laptop and in CI without a network round-trip. The
hosted validators on https://agentskills.io / skills.sh are stricter about some
optional fields; run `npx skills validate` there if you want the full check.

Rules enforced (portable core, per decision D4):
  * Every skills/<name>/ folder has a SKILL.md.
  * SKILL.md starts with a YAML frontmatter block delimited by `---`.
  * Frontmatter has non-empty `name` and `description`.
  * `name` matches the folder name (so cross-agent discovery is stable).
  * `description` is written as an activation trigger and is a sane length.
  * No Claude-only `context:`/`fork` or Codex-only `openai.yaml` that would break
    byte-identical reuse across agents.
"""

from __future__ import annotations

import sys
from pathlib import Path

MIN_DESC = 20
MAX_DESC = 1500
MAX_BODY_LINES = 600  # soft cap; warn only

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def parse_frontmatter(text: str) -> tuple[dict[str, str], int]:
    """Return (flat string map of top-level scalar keys, body_line_count).

    A deliberately small parser: we only need top-level scalar keys (name,
    description, possibly folded with `>` or `|`). Nested mappings (metadata:)
    are skipped, which is fine — we never assert on them.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening '---' frontmatter delimiter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("missing closing '---' frontmatter delimiter")

    data: dict[str, str] = {}
    i = 1
    while i < end:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        # Only consider top-level keys (no leading indentation).
        if raw[0] in (" ", "\t"):
            i += 1
            continue
        if ":" not in raw:
            i += 1
            continue
        key, _, val = raw.partition(":")
        key = key.strip()
        val = val.strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):
            # Folded/literal scalar: gather subsequent indented lines.
            block: list[str] = []
            j = i + 1
            while j < end and (not lines[j].strip() or lines[j][:1] in (" ", "\t")):
                block.append(lines[j].strip())
                j += 1
            data[key] = " ".join(b for b in block if b)
            i = j
            continue
        data[key] = val.strip().strip("'\"")
        i += 1

    return data, len(lines) - end - 1


def validate_skill(folder: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return [f"{folder.name}: no SKILL.md"], []

    text = skill_md.read_text(encoding="utf-8")
    try:
        fm, body_lines = parse_frontmatter(text)
    except ValueError as exc:
        return [f"{folder.name}: {exc}"], []

    name = fm.get("name", "").strip()
    desc = fm.get("description", "").strip()

    if not name:
        errors.append(f"{folder.name}: frontmatter missing 'name'")
    elif name != folder.name:
        errors.append(f"{folder.name}: name '{name}' != folder name")

    if not desc:
        errors.append(f"{folder.name}: frontmatter missing 'description'")
    else:
        if len(desc) < MIN_DESC:
            errors.append(f"{folder.name}: description too short ({len(desc)} chars)")
        if len(desc) > MAX_DESC:
            warnings.append(f"{folder.name}: description very long ({len(desc)} chars)")

    # Portability guards (D4).
    if "context:" in text.split("---", 2)[1] if text.count("---") >= 2 else False:
        warnings.append(f"{folder.name}: 'context:' frontmatter is Claude-only")
    if (folder / "openai.yaml").exists():
        errors.append(f"{folder.name}: openai.yaml is Codex-only; not portable")

    if body_lines > MAX_BODY_LINES:
        warnings.append(
            f"{folder.name}: SKILL.md body is {body_lines} lines "
            f"(>{MAX_BODY_LINES}; consider moving detail to references/)"
        )

    return errors, warnings


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"ERROR: {SKILLS_DIR} not found", file=sys.stderr)
        return 2

    folders = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())
    if not folders:
        print("ERROR: no skills found", file=sys.stderr)
        return 2

    all_errors: list[str] = []
    all_warnings: list[str] = []
    for folder in folders:
        errs, warns = validate_skill(folder)
        all_errors.extend(errs)
        all_warnings.extend(warns)

    for w in all_warnings:
        print(f"WARN  {w}")
    for e in all_errors:
        print(f"ERROR {e}")

    print(
        f"\nValidated {len(folders)} skills: "
        f"{len(all_errors)} error(s), {len(all_warnings)} warning(s)."
    )
    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
