#!/usr/bin/env python3
"""Validate the parstack repo layout.

Checks:
- every skills/* dir has SKILL.md with YAML frontmatter: `name` equal to the
  dir name (lowercase kebab, <=64 chars) and a non-empty `description`
  (<=1024 chars)
- no SKILL.md at the repo root
- .claude-plugin/marketplace.json and plugin.json parse; the marketplace lists
  plugin "parstack" with source "./"; plugin.json name is "parstack"
- every `${CLAUDE_SKILL_DIR}/scripts/X` referenced in a SKILL.md exists
- scripts under skills/*/scripts import only the standard library or modules
  local to the same scripts tree
- test files are reachable by `unittest discover -s skills` (every dir from
  the skill dir down to the test file has __init__.py)

Exit 0 when clean (warnings allowed), 1 on problems, 2 on usage error.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

PLUGIN_NAME = "parstack"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
SCRIPT_REF_RE = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/scripts/([^\s\"'`)\]}>]+)")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$")
IGNORED_DIRS = {"__pycache__"}


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.skills: list[str] = []

    def error(self, msg: str) -> None:
        self.problems.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


# --- frontmatter -----------------------------------------------------------

def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    # strip a trailing comment on plain scalars
    return re.sub(r"\s+#.*$", "", value)


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """Parse top-level keys of a leading `---` YAML block.

    Supports plain, quoted, folded (>) and literal (|) scalars, plain scalars
    continued on indented lines, and skips nested mappings/lists (stored as
    the raw indented text). Returns None when there is no frontmatter block.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None

    body = lines[1:end]
    result: dict[str, object] = {}
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " \t":
            # stray indented line with no owning key
            i += 1
            continue
        m = KEY_RE.match(line)
        if not m:
            i += 1
            continue
        key, raw = m.group(1), (m.group(2) or "").strip()
        i += 1
        block: list[str] = []
        while i < len(body) and (not body[i].strip() or body[i][0] in " \t"):
            block.append(body[i])
            i += 1
        while block and not block[-1].strip():
            block.pop()

        if raw[:1] in ("|", ">"):
            stripped = [b.strip() for b in block]
            if raw[0] == "|":
                result[key] = "\n".join(stripped).strip()
            else:
                result[key] = " ".join(s for s in stripped if s).strip()
        elif raw:
            if block and raw[0] not in "\"'":
                raw = " ".join([raw] + [b.strip() for b in block if b.strip()])
            result[key] = _unquote(raw)
        else:
            # nested mapping or list; keep raw text so callers can see it is not a string
            result[key] = {"_raw": "\n".join(block)} if block else ""
    return result


# --- checks ----------------------------------------------------------------

def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def check_root(root: Path, report: Report) -> None:
    for entry in root.iterdir():
        if entry.is_file() and entry.name.lower() == "skill.md":
            report.error(f"{entry.name} must not exist at the repo root")


def _load_json(path: Path, root: Path, report: Report) -> object | None:
    if not path.is_file():
        report.error(f"{_rel(path, root)} is missing")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.error(f"{_rel(path, root)} does not parse: {exc}")
        return None


def check_manifests(root: Path, report: Report) -> None:
    plugin_dir = root / ".claude-plugin"
    market = _load_json(plugin_dir / "marketplace.json", root, report)
    if market is not None:
        if not isinstance(market, dict):
            report.error(".claude-plugin/marketplace.json must be a JSON object")
        else:
            if market.get("name") != PLUGIN_NAME:
                report.error(f'.claude-plugin/marketplace.json name must be "{PLUGIN_NAME}"')
            owner = market.get("owner")
            if not isinstance(owner, dict) or not owner.get("name"):
                report.error(".claude-plugin/marketplace.json needs owner.name")
            plugins = market.get("plugins")
            if not isinstance(plugins, list):
                report.error(".claude-plugin/marketplace.json plugins must be a list")
            else:
                entry = next(
                    (p for p in plugins if isinstance(p, dict) and p.get("name") == PLUGIN_NAME),
                    None,
                )
                if entry is None:
                    report.error(f'.claude-plugin/marketplace.json must list plugin "{PLUGIN_NAME}"')
                elif entry.get("source") != "./":
                    report.error(
                        f'.claude-plugin/marketplace.json plugin "{PLUGIN_NAME}" source must be "./"'
                        f" (got {entry.get('source')!r})"
                    )

    plugin = _load_json(plugin_dir / "plugin.json", root, report)
    if plugin is not None:
        if not isinstance(plugin, dict):
            report.error(".claude-plugin/plugin.json must be a JSON object")
        elif plugin.get("name") != PLUGIN_NAME:
            report.error(
                f'.claude-plugin/plugin.json name must be "{PLUGIN_NAME}"'
                f" (got {plugin.get('name')!r})"
            )


def _local_module_names(start: Path, scripts_root: Path) -> set[str]:
    """Module names importable from files in `start`, walking up to scripts_root."""
    names: set[str] = set()
    current = start
    while True:
        for entry in current.iterdir():
            if entry.is_file() and entry.suffix == ".py":
                names.add(entry.stem)
            elif entry.is_dir() and entry.name not in IGNORED_DIRS:
                names.add(entry.name)
        if current == scripts_root or scripts_root not in current.parents:
            break
        current = current.parent
    return names


def imported_top_modules(source: str) -> list[tuple[str, int]]:
    """Return (top-level module, line) for every absolute import in source."""
    tree = ast.parse(source)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def check_script_imports(skill_dir: Path, root: Path, report: Report) -> None:
    scripts_root = skill_dir / "scripts"
    if not scripts_root.is_dir():
        return
    stdlib = set(sys.stdlib_module_names)
    for py in sorted(scripts_root.rglob("*.py")):
        if any(part in IGNORED_DIRS for part in py.parts):
            continue
        try:
            modules = imported_top_modules(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            report.error(f"{_rel(py, root)} does not parse: {exc}")
            continue
        local = _local_module_names(py.parent, scripts_root)
        for module, line in modules:
            if module in stdlib or module in local:
                continue
            report.error(
                f"{_rel(py, root)}:{line} imports non-stdlib module '{module}'"
            )


def check_test_discovery(skill_dir: Path, root: Path, report: Report) -> None:
    missing: set[Path] = set()
    for test in skill_dir.rglob("test_*.py"):
        if any(part in IGNORED_DIRS for part in test.parts):
            continue
        current = test.parent
        while True:
            if not (current / "__init__.py").is_file():
                missing.add(current)
            if current == skill_dir:
                break
            current = current.parent
    for d in sorted(missing):
        report.error(
            f"{_rel(d, root)} needs __init__.py so `unittest discover -s skills` finds its tests"
        )


def check_skill(skill_dir: Path, root: Path, report: Report) -> None:
    name = skill_dir.name
    rel = _rel(skill_dir, root)
    if not NAME_RE.match(name):
        report.error(f"{rel}: dir name must be lowercase kebab-case")
    if len(name) > NAME_MAX:
        report.error(f"{rel}: dir name longer than {NAME_MAX} chars")

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        report.error(f"{rel}: missing SKILL.md")
        return
    text = skill_md.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    if fm is None:
        report.error(f"{rel}/SKILL.md: missing YAML frontmatter (--- block at top)")
    else:
        fm_name = fm.get("name")
        if not isinstance(fm_name, str) or not fm_name:
            report.error(f"{rel}/SKILL.md: frontmatter needs `name`")
        elif fm_name != name:
            report.error(f"{rel}/SKILL.md: frontmatter name '{fm_name}' must equal dir name '{name}'")
        desc = fm.get("description")
        if not isinstance(desc, str) or not desc.strip():
            report.error(f"{rel}/SKILL.md: frontmatter needs a non-empty `description`")
        elif len(desc) > DESCRIPTION_MAX:
            report.error(
                f"{rel}/SKILL.md: description is {len(desc)} chars (max {DESCRIPTION_MAX})"
            )

    scripts_root = (skill_dir / "scripts").resolve()
    for ref in sorted(set(SCRIPT_REF_RE.findall(text))):
        ref = ref.rstrip(".,;:")
        target = (skill_dir / "scripts" / ref).resolve()
        if scripts_root not in target.parents:
            report.error(f"{rel}/SKILL.md: script reference escapes scripts/: {ref}")
        elif not target.is_file():
            report.error(f"{rel}/SKILL.md: referenced script scripts/{ref} does not exist")

    check_script_imports(skill_dir, root, report)
    check_test_discovery(skill_dir, root, report)


def check_skills(root: Path, report: Report) -> None:
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        report.warn("no skills/ dir yet")
        return
    dirs = []
    for entry in sorted(skills_dir.iterdir()):
        if entry.name.startswith(".") or entry.name in IGNORED_DIRS:
            continue
        if entry.is_dir():
            dirs.append(entry)
        else:
            report.warn(f"{_rel(entry, root)}: stray file in skills/ (expected only skill dirs)")
    if not dirs:
        report.warn("skills/ has no skill dirs yet")
        return

    readme = root / "README.md"
    readme_text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    for d in dirs:
        report.skills.append(d.name)
        check_skill(d, root, report)
        if d.name not in readme_text:
            report.warn(f"{_rel(d, root)}: not mentioned in README.md")


def validate(root: Path) -> Report:
    report = Report()
    check_root(root, report)
    check_manifests(root, report)
    check_skills(root, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the parstack repo layout.")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="repo root (default: parent of tools/)",
    )
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"error: --root {root} is not a directory", file=sys.stderr)
        return 2

    report = validate(root.resolve())
    ok = not report.problems
    if args.json:
        print(json.dumps(
            {"ok": ok, "skills": report.skills, "problems": report.problems, "warnings": report.warnings},
            indent=2,
        ))
    else:
        for p in report.problems:
            print(f"error: {p}")
        for w in report.warnings:
            print(f"warning: {w}")
        status = "ok" if ok else "FAIL"
        print(
            f"validate: {status} - {len(report.skills)} skill(s), "
            f"{len(report.problems)} problem(s), {len(report.warnings)} warning(s)"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
