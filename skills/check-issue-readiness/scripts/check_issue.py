#!/usr/bin/env python3
"""Lint one issue for readiness: criteria, proof, scope, dependencies, size, vague words.

Sources: a GitHub issue via gh, a markdown file, or an exported JSON issue
(GitHub, Linear, or Jira-like fields).

Verdicts: ready, ready-with-assumptions, not-ready.
Exit codes: 0 ready or ready-with-assumptions (1 with --strict), 1 not-ready,
2 usage or input error (including gh not found).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

MAX_ITEMS = 5

SECTION_KINDS = [
    ("out_of_scope", r"out of scope|non-goals?|not in scope|excluded|won'?t do"),
    ("open_questions", r"open questions?|unknowns|questions"),
    ("acceptance", r"acceptance|done when|definition of done|success criteria|criteria|"
                   r"expected behaviou?r|requirements"),
    ("dependencies", r"dependenc|depends on|blocked by|prerequisite|blockers"),
    ("proof", r"proof|verification|verify|test plan|testing|how to test"),
    ("scope", r"scope|tasks|changes|implementation|deliverables|what to (?:do|build)"),
]
ATX_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
BOLD_RE = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
LABEL_RE = re.compile(r"^\s*([A-Z][A-Za-z /'-]{2,40}):\s*$")
ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*\S)\s*$")
GWT_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(Given|Scenario)\b", re.IGNORECASE)
DEP_RE = re.compile(
    r"\b(?:depends on|dependent on|blocked by|blocked on|requires|after)\s*:?\s*"
    r"((?:(?:[\w.-]+/[\w.-]+)?#\d+|[A-Z][A-Z0-9]+-\d+)"
    r"(?:\s*(?:,|and|&)\s*(?:(?:[\w.-]+/[\w.-]+)?#\d+|[A-Z][A-Z0-9]+-\d+))*)",
    re.IGNORECASE,
)
REF_RE = re.compile(r"(?:[\w.-]+/[\w.-]+)?#\d+|\b[A-Z][A-Z0-9]+-\d+\b")
PROOF_MARKER_RE = re.compile(
    r"\b(?:proof|proven by|proved by|test|tests|verify|verified by|check|cmd|command)\s*:\s*(.*)",
    re.IGNORECASE,
)
PROOF_TOKEN_RE = re.compile(r"`[^`]+`|\btest_\w+|\w+\.(?:test|spec)\.\w+|\w+::\w+|\w+Tests?\.\w+")
VAGUE_PROOF_RE = re.compile(r"^(?:manual\w*|by hand|works|tbd|todo|n/?a|none|-|\?)?\.?$", re.IGNORECASE)
UNRESOLVED_RE = re.compile(r"\bTBD\b|\bTODO\b|\?\?+|\b[Tt]o be (?:decided|determined|confirmed)\b")
NONE_RE = re.compile(r"^\s*(?:none|n/?a|no dependencies|nothing|-)\.?\s*$", re.IGNORECASE)
VAGUE_WORDS = [
    "etc", "and so on", "various", "somehow", "appropriate", "appropriately", "as needed",
    "properly", "correctly", "fast", "quickly", "user-friendly", "intuitive", "robust",
    "seamless", "seamlessly", "easy", "easily", "better", "improve", "improved", "optimize",
    "clean up", "should work", "nice", "good", "efficient", "flexible", "scalable", "modern",
]
VAGUE_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in VAGUE_WORDS) + r")\b", re.IGNORECASE)
BLOCKING_LABEL_RE = re.compile(r"^(?:blocked|needs[- ]?info|needs[- ]?design|question|"
                               r"wontfix|won't fix|duplicate|invalid)$", re.IGNORECASE)


class InputError(Exception):
    pass


# ---------------------------------------------------------------- sources

def from_github(number: str, repo: str | None, runner=subprocess.run, which=shutil.which) -> dict:
    if not which("gh"):
        raise InputError("skipped: gh not found")
    cmd = ["gh", "issue", "view", str(number), "--json", "number,title,body,labels,url"]
    if repo:
        cmd += ["--repo", repo]
    proc = runner(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise InputError(f"gh failed: {(proc.stderr or proc.stdout).strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise InputError(f"gh returned invalid JSON: {exc}") from exc
    issue = from_exported(data)
    issue["source"] = data.get("url") or f"github:{repo or ''}#{number}"
    return issue


def flatten_text(node) -> str:
    """Flatten Jira ADF or other nested rich text into plain lines."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        inner = flatten_text(node.get("content", []))
        kind = node.get("type")
        if kind == "heading":
            return "#" * int(node.get("attrs", {}).get("level", 2)) + " " + inner + "\n"
        if kind == "listItem":
            return "- " + inner.strip() + "\n"
        if kind in ("paragraph", "taskItem"):
            prefix = "- [ ] " if kind == "taskItem" else ""
            return prefix + inner + "\n"
        return inner
    return ""


def label_names(labels) -> list[str]:
    out = []
    if isinstance(labels, dict):
        labels = labels.get("nodes") or labels.get("items") or []
    for lab in labels or []:
        name = lab.get("name") if isinstance(lab, dict) else lab
        if isinstance(name, str) and name:
            out.append(name)
    return out


def from_exported(data) -> dict:
    if isinstance(data, list):
        if len(data) != 1:
            raise InputError(f"expected one issue, got a list of {len(data)}")
        data = data[0]
    if not isinstance(data, dict):
        raise InputError("issue JSON must be an object")
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    title = data.get("title") or data.get("summary") or fields.get("summary") or data.get("name") or ""
    body = data.get("body")
    if body is None:
        body = data.get("description", fields.get("description", ""))
    labels = label_names(data.get("labels", fields.get("labels", [])))
    return {"title": str(title).strip(), "body": flatten_text(body) if body else "",
            "labels": labels, "source": data.get("url") or data.get("key") or "json"}


def from_markdown(text: str, fallback_title: str) -> dict:
    lines = text.splitlines()
    title = fallback_title
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = re.match(r"^\s{0,3}#\s+(.*\S)\s*$", line)
        if m:
            title = m.group(1)
            lines = lines[:i] + lines[i + 1:]
        break
    return {"title": title, "body": "\n".join(lines), "labels": [], "source": "markdown"}


# ---------------------------------------------------------------- parsing

def strip_code(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def classify(heading: str) -> str:
    h = heading.lower()
    for kind, pattern in SECTION_KINDS:
        if re.search(pattern, h):
            return kind
    return "other"


def split_sections(body: str) -> list[dict]:
    sections = [{"heading": "", "kind": "other", "lines": []}]
    for line in body.splitlines():
        m = ATX_RE.match(line) or BOLD_RE.match(line) or LABEL_RE.match(line)
        if m:
            heading = m.group(1).strip().strip("*").strip()
            sections.append({"heading": heading, "kind": classify(heading), "lines": []})
        else:
            sections[-1]["lines"].append(line)
    return sections


def parse_items(lines: list[str]) -> list[dict]:
    """Top-level list items with nested item text attached as children."""
    items: list[dict] = []
    base = 0
    for line in lines:
        m = ITEM_RE.match(line)
        if not m:
            if items and line.strip() and line.startswith((" ", "\t")):
                items[-1]["children"].append(line.strip())
            continue
        indent = len(m.group(1).expandtabs(4))
        if not items or indent <= base + 1:
            base = indent if not items else min(base, indent)
            items.append({"text": m.group(3), "checkbox": m.group(2) is not None,
                          "children": []})
        else:
            items[-1]["children"].append(m.group(3))
    return items


def find_proof(item: dict) -> str | None:
    for text in [item["text"]] + item["children"]:
        m = PROOF_MARKER_RE.search(text)
        if m:
            value = m.group(1).strip()
            while value.endswith(")") and value.count(")") > value.count("("):
                value = value[:-1].rstrip()
            if value and not VAGUE_PROOF_RE.match(value):
                return value
            continue
        t = PROOF_TOKEN_RE.search(text)
        if t:
            return t.group(0)
    return None


def parse_issue(issue: dict) -> dict:
    body = strip_code(issue.get("body") or "")
    sections = split_sections(body)
    by_kind: dict[str, list[dict]] = {}
    for s in sections:
        by_kind.setdefault(s["kind"], []).append(s)

    def items_of(kind):
        return [it for s in by_kind.get(kind, []) for it in parse_items(s["lines"])]

    criteria_items = items_of("acceptance")
    criteria_source = "section" if criteria_items else None
    if not criteria_items:
        skip = {"scope", "out_of_scope", "dependencies", "proof", "open_questions"}
        criteria_items = [it for s in sections if s["kind"] not in skip
                          for it in parse_items(s["lines"]) if it["checkbox"]]
        criteria_source = "checkboxes" if criteria_items else None
    if not criteria_items:
        gwt = [line.strip() for line in body.splitlines() if GWT_RE.match(line)]
        criteria_items = [{"text": g, "checkbox": False, "children": []} for g in gwt]
        criteria_source = "given-when-then" if criteria_items else None

    dep_refs: list[str] = []
    for m in DEP_RE.finditer(body):
        for ref in REF_RE.findall(m.group(1)):
            if ref not in dep_refs:
                dep_refs.append(ref)
    dep_sections = by_kind.get("dependencies", [])
    deps_none = False
    for s in dep_sections:
        text = "\n".join(s["lines"]).strip()
        for ref in REF_RE.findall(text):
            if ref not in dep_refs:
                dep_refs.append(ref)
        items = parse_items(s["lines"])
        if (items and all(NONE_RE.match(it["text"]) for it in items)) or NONE_RE.match(text or "-"):
            deps_none = True

    open_q = []
    for s in by_kind.get("open_questions", []):
        items = parse_items(s["lines"])
        texts = [it["text"] for it in items] or [ln.strip() for ln in s["lines"] if ln.strip()]
        open_q += [t for t in texts if not NONE_RE.match(t)]

    return {
        "criteria": [{"text": it["text"], "proof": find_proof(it)} for it in criteria_items],
        "criteria_source": criteria_source,
        "scope": [it["text"] for it in items_of("scope")],
        "has_scope_section": "scope" in by_kind,
        "out_of_scope": [it["text"] for it in items_of("out_of_scope")],
        "has_out_of_scope_section": "out_of_scope" in by_kind,
        "proof_section_items": [it["text"] for it in items_of("proof")]
        + [ln.strip() for s in by_kind.get("proof", []) for ln in s["lines"]
           if ln.strip() and not ITEM_RE.match(ln)],
        "dependencies": dep_refs,
        "dependencies_none": deps_none and not dep_refs,
        "open_questions": open_q,
        "unresolved": sorted({m.group(0) for m in UNRESOLVED_RE.finditer(body)}),
        "headings": [s["heading"] for s in sections if s["heading"]],
    }


# ---------------------------------------------------------------- verdict

def assess(issue: dict, max_items: int = MAX_ITEMS) -> dict:
    p = parse_issue(issue)
    missing: list[str] = []
    assumptions: list[str] = []

    if not issue.get("title"):
        missing.append("no title")
    crit = p["criteria"]
    if not crit:
        missing.append('no acceptance criteria: add an "## Acceptance criteria" list of '
                       "observable outcomes")
    if len(crit) > max_items:
        missing.append(f"too big: {len(crit)} acceptance criteria (max {max_items}); "
                       "slice it into smaller issues")
    if len(p["scope"]) > max_items:
        missing.append(f"too big: {len(p['scope'])} scope items (max {max_items}); "
                       "slice it into smaller issues")
    if p["unresolved"]:
        missing.append("unresolved markers: " + ", ".join(p["unresolved"]))
    for q in p["open_questions"]:
        missing.append(f"open question: {q}")
    for lab in issue.get("labels", []):
        if BLOCKING_LABEL_RE.match(lab):
            missing.append(f'label "{lab}" marks the issue as not ready')

    unproven = [c for c in crit if not c["proof"]]
    if crit and len(unproven) == len(crit) and not p["proof_section_items"]:
        missing.append("no proof named for any criterion: add a test name, check, or "
                       "command per criterion (e.g. '- proof: test_rejects_expired_token')")
    elif unproven and p["proof_section_items"]:
        assumptions.append(f"{len(unproven)} criteria have no inline proof; assume the "
                           "Testing/Verification section proves them")
    else:
        for c in unproven:
            assumptions.append(f'no proof for "{c["text"]}"; assume a new automated test '
                               "named after it proves it")

    if not p["has_scope_section"]:
        assumptions.append("no scope section; assume scope is only what the criteria require")
    if not p["has_out_of_scope_section"]:
        assumptions.append("no out-of-scope section; assume anything not in the criteria "
                           "is out of scope")
    notes: list[str] = []
    if p["dependencies"]:
        notes.append("confirm these are merged before starting: " + ", ".join(p["dependencies"]))
    elif not p["dependencies_none"]:
        assumptions.append("no dependencies stated; assume none")
    for c in crit:
        words = sorted({m.group(1).lower() for m in VAGUE_RE.finditer(c["text"])})
        if words:
            assumptions.append(f'vague wording ({", ".join(words)}) in "{c["text"]}"; '
                               "assume its proof sets the bar")

    if missing:
        verdict = "not-ready"
    elif assumptions:
        verdict = "ready-with-assumptions"
    else:
        verdict = "ready"
    return {
        "verdict": verdict,
        "title": issue.get("title", ""),
        "source": issue.get("source", ""),
        "labels": issue.get("labels", []),
        "missing": missing,
        "assumptions": assumptions,
        "notes": notes,
        "detected": p,
    }


def format_human(r: dict) -> str:
    d = r["detected"]
    proven = sum(1 for c in d["criteria"] if c["proof"])
    lines = [f"{r['verdict']}: {r['title'] or '(no title)'}"]
    if r["missing"]:
        lines.append("missing:")
        lines += [f"  - {m}" for m in r["missing"]]
    if r["assumptions"]:
        lines.append("assumptions:")
        lines += [f"  - {a}" for a in r["assumptions"]]
    lines += [f"note: {n}" for n in r["notes"]]
    deps = ", ".join(d["dependencies"]) or ("none" if d["dependencies_none"] else "not stated")
    lines.append(f"detected: {len(d['criteria'])} criteria ({proven} with proof), "
                 f"{len(d['scope'])} scope items, {len(d['out_of_scope'])} out-of-scope items, "
                 f"dependencies: {deps}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, runner=subprocess.run, which=shutil.which) -> int:
    ap = argparse.ArgumentParser(description="Check whether an issue is ready to start.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--gh", metavar="NUMBER", help="GitHub issue number (needs gh)")
    src.add_argument("--file", metavar="PATH", help="markdown issue file (first # heading is the title)")
    src.add_argument("--json-file", metavar="PATH",
                     help="exported issue JSON (title/body, title/description, or fields.summary/description)")
    ap.add_argument("--repo", help="owner/name for --gh (default: current repo)")
    ap.add_argument("--max-items", type=int, default=MAX_ITEMS,
                    help=f"max criteria or scope items before suggesting a split (default {MAX_ITEMS})")
    ap.add_argument("--strict", action="store_true", help="exit 1 on ready-with-assumptions too")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = ap.parse_args(argv)

    try:
        if args.gh:
            issue = from_github(args.gh, args.repo, runner=runner, which=which)
        else:
            path = Path(args.file or args.json_file)
            if not path.is_file():
                raise InputError(f"file not found: {path}")
            text = path.read_text(encoding="utf-8")
            if args.file:
                issue = from_markdown(text, path.stem)
            else:
                try:
                    issue = from_exported(json.loads(text))
                except json.JSONDecodeError as exc:
                    raise InputError(f"invalid JSON: {exc}") from exc
    except InputError as exc:
        msg = str(exc)
        if args.json:
            key = "skipped" if msg.startswith("skipped:") else "input_error"
            print(json.dumps({key: msg.removeprefix("skipped: ")}))
        else:
            print(msg if msg.startswith("skipped:") else f"input error: {msg}", file=sys.stderr)
        return 2

    result = assess(issue, args.max_items)
    print(json.dumps(result, indent=2) if args.json else format_human(result))
    if result["verdict"] == "not-ready":
        return 1
    if result["verdict"] == "ready-with-assumptions" and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
