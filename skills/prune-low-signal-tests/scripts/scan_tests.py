#!/usr/bin/env python3
"""Deterministic groundwork for pruning low-signal tests.

Modes:
  list    test files under a path, grouped by area (leading dir segments), with
          file and test counts, so a pass can take one area at a time
  diff    test files changed in a diff (`--base REF` runs
          `git diff base...HEAD`; `--diff-file FILE` or `-` reads a unified
          diff) and the test names added or removed in each
  smells  cheap static hints per test file (absence assertions,
          self-comparisons, call counts, private access, type-only checks,
          conditional skips, copied class strings). Hints need reading; they
          are not verdicts.

Test names are best effort: it()/test()/describe() in JS/TS, def test_* and
Test* classes in Python, func Test* in Go. Other languages are listed with a
null test count.

Exit codes: 0 ok (hints never fail the run), 2 usage or input error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv", "__pycache__",
             "vendor", "target", "coverage", ".next"}
JS_EXT = r"(?:js|jsx|ts|tsx|mjs|cjs|mts|cts)"
TEST_FILE_RES = [
    re.compile(rf"\.(?:test|spec)\.{JS_EXT}$"),
    re.compile(rf"(?:^|/)__tests__/.+\.{JS_EXT}$"),
    re.compile(r"(?:^|/)test_[^/]*\.py$"),
    re.compile(r"_test\.py$"),
    re.compile(r"_test\.go$"),
    re.compile(r"Tests?\.(?:cs|java|kt|php|swift)$"),
    re.compile(r"_(?:spec|test)\.rb$"),
    re.compile(r"_test\.exs$"),
]
NOTE = "Hints, not verdicts: read each test before putting it in a category."

# ---------------------------------------------------------------- test names

JS_NAME_RE = re.compile(
    r"(?<![\w.$])(describe|context|suite|it|test)"
    r"(?:\.(?:only|skip|todo|concurrent|failing))*\s*\(\s*"
    r"(['\"`])((?:\\.|(?!\2).)*)\2"
)
PY_TEST_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
PY_SUITE_RE = re.compile(r"^\s*class\s+(Test\w*|\w+\(.*TestCase.*\))\s*[:(]")
GO_TEST_RE = re.compile(r"^func\s+(Test\w*)\s*\(")


def language(path: str) -> str | None:
    if re.search(rf"\.{JS_EXT}$", path):
        return "js"
    if path.endswith(".py"):
        return "python"
    if path.endswith(".go"):
        return "go"
    return None


def names_in_line(line: str, lang: str | None) -> list[tuple[str, str]]:
    """(kind, name) pairs declared on one line; kind is 'test' or 'suite'."""
    if lang == "js":
        out = []
        for m in JS_NAME_RE.finditer(line):
            kind = "suite" if m.group(1) in ("describe", "context", "suite") else "test"
            out.append((kind, m.group(3)))
        return out
    if lang == "python":
        m = PY_TEST_RE.match(line)
        if m:
            return [("test", m.group(1))]
        m = PY_SUITE_RE.match(line)
        if m:
            return [("suite", m.group(1).split("(")[0])]
        return []
    if lang == "go":
        m = GO_TEST_RE.match(line)
        return [("test", m.group(1))] if m else []
    return []


def count_tests(text: str, lang: str | None) -> int | None:
    if lang is None:
        return None
    return sum(1 for line in text.splitlines() for kind, _ in names_in_line(line, lang) if kind == "test")


# ---------------------------------------------------------------- files

def is_test_file(path: str) -> bool:
    return any(rx.search(path) for rx in TEST_FILE_RES)


def list_files(root: Path) -> list[str]:
    """Files under root, relative and sorted. Uses git when root is in a work tree."""
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            proc = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                                   "--exclude-standard"], capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                return sorted({f for f in proc.stdout.splitlines() if f and (root / f).is_file()})
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            out.append((Path(dirpath) / name).relative_to(root).as_posix())
    return sorted(out)


def test_files(target: Path) -> tuple[Path, list[str]]:
    """(base dir, test files relative to it). A file target is its own single entry."""
    if target.is_file():
        return target.parent, [target.name]
    return target, [f for f in list_files(target) if is_test_file(f)]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def area_of(path: str, depth: int) -> str:
    dirs = path.split("/")[:-1]
    return "/".join(dirs[:depth]) or "."


# ---------------------------------------------------------------- list

def run_list(target: Path, shown: str, depth: int) -> dict:
    base, files = test_files(target)
    areas: dict[str, dict] = {}
    for rel in files:
        tests = count_tests(read_text(base / rel), language(rel))
        area = areas.setdefault(area_of(rel, depth), {"files": [], "tests": 0, "uncounted_files": 0})
        area["files"].append({"path": rel, "tests": tests})
        if tests is None:
            area["uncounted_files"] += 1
        else:
            area["tests"] += tests
    out_areas = [
        {"area": name, "file_count": len(a["files"]), "tests": a["tests"],
         "uncounted_files": a["uncounted_files"], "files": a["files"]}
        for name, a in sorted(areas.items())
    ]
    return {
        "mode": "list", "root": shown, "depth": depth,
        "total_files": len(files), "total_tests": sum(a["tests"] for a in out_areas),
        "areas": out_areas,
    }


def human_list(r: dict) -> str:
    lines = [f"{r['total_files']} test file(s), {r['total_tests']} counted test(s) in "
             f"{len(r['areas'])} area(s) under {r['root']}"]
    width = max((len(a["area"]) for a in r["areas"]), default=0)
    for a in r["areas"]:
        extra = f"  ({a['uncounted_files']} file(s) not counted)" if a["uncounted_files"] else ""
        lines.append(f"  {a['area']:<{width}}  {a['file_count']:>4} file(s)  {a['tests']:>5} test(s){extra}")
    if r["areas"]:
        lines.append("Prune one area per pass.")
    return "\n".join(lines)


# ---------------------------------------------------------------- diff

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(path: str) -> str | None:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path[:2] in ("a/", "b/"):
        path = path[2:]
    return path


def parse_diff(text: str) -> list[dict]:
    """Parse a unified diff into per-file records with added and removed lines."""
    files: list[dict] = []
    cur: dict | None = None
    old_no = new_no = old_left = new_left = 0

    def start(old: str | None, new: str | None) -> dict:
        rec = {"old": old, "new": new, "status": "modified", "added": [], "removed": []}
        files.append(rec)
        return rec

    for line in text.splitlines():
        if cur is not None and (old_left > 0 or new_left > 0):
            if line.startswith("+"):
                cur["added"].append((new_no, line[1:]))
                new_no, new_left = new_no + 1, new_left - 1
                continue
            if line.startswith("-"):
                cur["removed"].append((old_no, line[1:]))
                old_no, old_left = old_no + 1, old_left - 1
                continue
            if line.startswith(" ") or line == "":
                old_no, new_no = old_no + 1, new_no + 1
                old_left, new_left = old_left - 1, new_left - 1
                continue
            if line.startswith("\\"):
                continue
            old_left = new_left = 0
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git (\S+) (\S+)$", line)
            cur = start(_strip_prefix(m.group(1)), _strip_prefix(m.group(2))) if m else start(None, None)
            continue
        if line.startswith("--- "):
            if cur is None or cur.get("_minus_seen"):
                cur = start(None, None)
            cur["_minus_seen"] = True
            cur["old"] = _strip_prefix(line[4:])
            if cur["old"] is None:
                cur["status"] = "added"
            continue
        if line.startswith("+++ ") and cur is not None:
            cur["new"] = _strip_prefix(line[4:])
            if cur["new"] is None:
                cur["status"] = "deleted"
            continue
        if cur is None:
            continue
        m = HUNK_RE.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
        elif line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from "):
            cur["old"], cur["status"] = line[len("rename from "):], "renamed"
        elif line.startswith("rename to "):
            cur["new"], cur["status"] = line[len("rename to "):], "renamed"
    for rec in files:
        rec.pop("_minus_seen", None)
        if rec["status"] == "deleted":
            rec["new"] = None
        elif rec["status"] == "added":
            rec["old"] = None
        elif rec["status"] == "modified" and rec["old"] and rec["new"] and rec["old"] != rec["new"]:
            rec["status"] = "renamed"
    return files


def _declared(lines: list[tuple[int, str]], lang: str | None) -> list[dict]:
    return [{"kind": kind, "name": name, "line": no}
            for no, text in lines for kind, name in names_in_line(text, lang)]


def _net(mine: list[dict], other: list[dict]) -> list[dict]:
    """Names in `mine` not cancelled by the same (kind, name) in `other` (an edited line, not a new test)."""
    pool: dict[tuple[str, str], int] = {}
    for d in other:
        key = (d["kind"], d["name"])
        pool[key] = pool.get(key, 0) + 1
    out = []
    for d in mine:
        key = (d["kind"], d["name"])
        if pool.get(key):
            pool[key] -= 1
        else:
            out.append(d)
    return out


def run_diff(text: str, source: str) -> dict:
    tests, removed_sources = [], []
    for rec in parse_diff(text):
        path = rec["new"] or rec["old"] or ""
        if not (is_test_file(path) or (rec["old"] and is_test_file(rec["old"]))):
            if rec["status"] in ("deleted", "renamed"):
                removed_sources.append({"path": rec["old"], "status": rec["status"], "new_path": rec["new"]})
            continue
        added = _declared(rec["added"], language(rec["new"] or ""))
        removed = _declared(rec["removed"], language(rec["old"] or ""))
        entry = {"path": path, "status": rec["status"],
                 "tests_added": _net(added, removed), "tests_removed": _net(removed, added)}
        if rec["status"] == "renamed":
            entry["old_path"] = rec["old"]
        tests.append(entry)
    tests.sort(key=lambda e: e["path"])
    removed_sources.sort(key=lambda e: e["path"] or "")
    return {
        "mode": "diff", "source": source,
        "totals": {
            "test_files": len(tests),
            "tests_added": sum(1 for e in tests for d in e["tests_added"] if d["kind"] == "test"),
            "tests_removed": sum(1 for e in tests for d in e["tests_removed"] if d["kind"] == "test"),
        },
        "test_files": tests,
        "moved_or_deleted_source_files": removed_sources,
    }


def human_diff(r: dict) -> str:
    t = r["totals"]
    lines = [f"{t['test_files']} test file(s) changed in {r['source']}: "
             f"+{t['tests_added']} / -{t['tests_removed']} test(s)"]
    for e in r["test_files"]:
        head = f"  {e['path']} ({e['status']}"
        head += f" from {e['old_path']})" if e.get("old_path") else ")"
        lines.append(head)
        for d in e["tests_added"]:
            lines.append(f"    + {d['kind']} \"{d['name']}\" (line {d['line']})")
        for d in e["tests_removed"]:
            lines.append(f"    - {d['kind']} \"{d['name']}\" (old line {d['line']})")
    if r["moved_or_deleted_source_files"]:
        lines.append("Source files moved or deleted (tests that still mention them may be past):")
        for s in r["moved_or_deleted_source_files"]:
            to = f" -> {s['new_path']}" if s["new_path"] else ""
            lines.append(f"  {s['status']}: {s['path']}{to}")
    return "\n".join(lines)


def git_diff(repo: Path, base: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), "diff", "--no-color", "--no-ext-diff", "-M",
                               f"{base}...HEAD"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"git diff failed: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(f"git diff {base}...HEAD failed: {proc.stderr.strip()}")
    return proc.stdout


# ---------------------------------------------------------------- smells

SMELLS: list[tuple[str, re.Pattern]] = [
    ("absence-assertion", re.compile(
        r"existsSync\(.*\)\s*\)\s*\.(?:toBe\(false\)|toBeFalsy\(\))"
        r"|\.not\.(?:toContain|toHaveProperty|toBeDefined|toMatch)\("
        r"|assert\s+not\s+(?:os\.path\.exists|hasattr|Path\(.*\)\.exists)"
        r"|assertFalse\(\s*(?:os\.path\.exists|hasattr|Path\(.*\)\.exists)"
        r"|assertNotIn\(|\bassert\s+.+\s+not\s+in\s+"
    )),
    ("self-comparison", re.compile(
        r"expect\((.+)\)\s*\.(?:toBe|toEqual|toStrictEqual)\(\1\)"
        r"|assert(?:Equal|Is)\(\s*(.+?)\s*,\s*\2\s*\)"
        r"|\bassert\s+(.+?)\s*==\s*\3\s*$"
    )),
    ("call-count", re.compile(
        r"toHaveBeenCalledTimes\(|\.callCount\b|\.call_count\b|assert_called_once"
        r"|verify\(.*times\(\d+\)|Times\.(?:Once|Never|Exactly)"
    )),
    ("private-access", re.compile(
        r"\bas\s+any\)\s*\.\w|\[\s*['\"]_\w+['\"]\s*\]"
        r"|\b(?!self\.|cls\.)[A-Za-z]\w*\._[a-z]\w*"
    )),
    ("type-only-check", re.compile(
        r"typeof\s+[\w.$\[\]'\"]+\s*===?\s*['\"]function['\"]"
        r"|expect\(\s*typeof\b.*\.toBe\(\s*['\"]function['\"]"
        r"|\.toBeInstanceOf\(\s*Function\s*\)"
        r"|\bassert(?:True\()?\s*callable\("
    )),
    ("skipped-or-gated", re.compile(
        r"\b(?:it|test|describe)\.(?:skipIf|runIf|skip)\("
        r"|(?<![\w.])x(?:it|describe)\(|@pytest\.mark\.skip(?:if)?\b|@(?:unittest\.)?skip(?:If|Unless)?\("
        r"|\bt\.Skip(?:f|Now)?\("
    )),
    ("copied-class-string", re.compile(
        r"(?:className|classList|toHaveClass|toHaveStyle|\bclass=|\.style\b|['\"]class['\"])"
        r".*['\"`](?:[^'\"`\s]+\s+){4,}[^'\"`\s]+['\"`]"
    )),
]


def smells_in(text: str) -> list[dict]:
    hints = []
    for no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "#", "*", "/*")):
            continue
        for kind, rx in SMELLS:
            if rx.search(line):
                hints.append({"line": no, "kind": kind, "text": stripped[:160]})
    return hints


def run_smells(target: Path, shown: str) -> dict:
    base, files = test_files(target)
    counts = {kind: 0 for kind, _ in SMELLS}
    flagged = []
    for rel in files:
        hints = smells_in(read_text(base / rel))
        for h in hints:
            counts[h["kind"]] += 1
        if hints:
            flagged.append({"path": rel, "hints": hints})
    return {
        "mode": "smells", "root": shown, "files_scanned": len(files),
        "files_with_hints": len(flagged), "hint_counts": counts, "files": flagged, "note": NOTE,
    }


def human_smells(r: dict) -> str:
    total = sum(r["hint_counts"].values())
    lines = [f"{total} hint(s) in {r['files_with_hints']} of {r['files_scanned']} test file(s) under {r['root']}"]
    for kind, n in r["hint_counts"].items():
        if n:
            lines.append(f"  {kind}: {n}")
    for f in r["files"]:
        lines.append(f"{f['path']}")
        for h in f["hints"]:
            lines.append(f"  {h['line']:>5}  {h['kind']:<20} {h['text']}")
    lines.append(r["note"])
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Groundwork for pruning low-signal tests.")
    sub = parser.add_subparsers(dest="mode", required=True)
    p_list = sub.add_parser("list", help="test files grouped by area, with counts")
    p_list.add_argument("path", nargs="?", default=".", help="dir to scan (default: .)")
    p_list.add_argument("--depth", type=int, default=2, help="dir segments that make an area (default: 2)")
    p_diff = sub.add_parser("diff", help="test files and test names changed in a diff")
    src = p_diff.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="diff base...HEAD in --repo")
    src.add_argument("--diff-file", help="unified diff file, or - for stdin")
    p_diff.add_argument("--repo", default=".", help="git repo for --base (default: .)")
    p_smells = sub.add_parser("smells", help="static hints per test file (not verdicts)")
    p_smells.add_argument("path", nargs="?", default=".", help="dir or test file to scan (default: .)")
    for p in (p_list, p_diff, p_smells):
        p.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args(argv)

    try:
        if args.mode == "diff":
            if args.base:
                repo = Path(args.repo)
                if not repo.is_dir():
                    raise ValueError(f"not a directory: {args.repo}")
                result = run_diff(git_diff(repo, args.base), f"git:{args.base}...HEAD")
            elif args.diff_file == "-":
                result = run_diff(sys.stdin.read(), "stdin")
            else:
                path = Path(args.diff_file)
                if not path.is_file():
                    raise ValueError(f"diff file not found: {args.diff_file}")
                result = run_diff(read_text(path), f"diff-file:{args.diff_file}")
            text = human_diff(result)
        else:
            target = Path(args.path)
            if not target.exists():
                raise ValueError(f"path not found: {args.path}")
            if args.mode == "list":
                if args.depth < 0:
                    raise ValueError("--depth must be 0 or more")
                if not target.is_dir():
                    raise ValueError(f"not a directory: {args.path}")
                result = run_list(target, args.path, args.depth)
                text = human_list(result)
            else:
                result = run_smells(target, args.path)
                text = human_smells(result)
    except ValueError as exc:
        if args.json:
            print(json.dumps({"mode": args.mode, "status": "error", "message": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
