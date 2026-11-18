#!/usr/bin/env python3
"""Order open issues into parallel run groups from their declared dependencies.

Sources: gh issue list, or a JSON / markdown list file.
Flags cycles, missing dependencies (closed, unknown, external), stale and blocked items.

Exit codes: 0 ordered, 1 cycles or unknown dependencies found, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Body dependency rules mirror check-issue-readiness (each skill is self-contained,
# so they are copied, not imported; tests/test_dependency_parity.py keeps them in step).
# Only explicit verbs make a dependency; "after", "builds on", "related" do not.
REF = r"(?:[\w.-]+/[\w.-]+)?#\d+|\b[A-Z][A-Z0-9]+-\d+\b"
# Slice ids (S1) and tracker keys (ABC-12) keep a list going but are not ordered here.
SLICE_ID = r"\b[A-Z][A-Za-z]{0,4}\d+(?:\.\d+)?\b"
DEP_TOKEN = rf"(?:{REF}|{SLICE_ID})"
DEP_LIST = rf"({DEP_TOKEN}(?:\s*(?:,|(?i:and)|&)\s*{DEP_TOKEN})*)"
DEP_RE = re.compile(r"(?i:\b(depends on|dependent on|blocked by|blocked on|requires))\s*:?\s*"
                    + DEP_LIST)
BARE_REF_RE = re.compile(rf"^\s*((?:{REF})(?:\s*(?:,|and|&)\s*(?:{REF}))*)")
NOT_DEP_RE = re.compile(r"\b(?:related|see also|builds on|follow-?up|duplicate)\b", re.IGNORECASE)
# Heading kinds in check-issue-readiness order; earlier kinds win, so "Acceptance
# criteria and dependencies" is not a Dependencies section. Only the last two matter here.
SECTION_KINDS = [
    ("out_of_scope", r"out of scope|non-goals?|not in scope|excluded|won'?t do"),
    ("open_questions", r"open questions?|unknowns|questions"),
    ("assumptions", r"assumptions?"),
    ("acceptance", r"acceptance|done when|definition of done|success criteria|criteria|"
                   r"expected behaviou?r|requirements"),
    ("dependencies", r"dependenc|depends on|blocked by|prerequisite|blockers"),
    ("related", r"related|see also|references|links"),
]
ATX_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
BOLD_RE = re.compile(r"^\s*\*\*(.+?)\*\*:?\s*$")
LABEL_RE = re.compile(r"^\s*([A-Z][A-Za-z /'-]{2,40}):\s*$")
ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
REF_RE = re.compile(r"(?:([\w.-]+/[\w.-]+))?#(\d+)")
DEP_LABEL_RE = re.compile(r"^(?:depends[- _]?on|blocked[- _]?by|after)[\s:/#_-]*#?(\d+)$", re.IGNORECASE)
MD_ITEM_RE = re.compile(r"^[-*+]\s+(?:\[([ xX])\]\s+)?#(\d+)\b[:.)-]?\s*(.*)$")
MD_LABELS_RE = re.compile(r"\(?\blabels?:\s*([^)\n]+)\)?", re.IGNORECASE)
MD_UPDATED_RE = re.compile(r"\(?\bupdated(?:At)?:\s*([0-9T:\-+.Z]+)\)?", re.IGNORECASE)


class InputError(Exception):
    pass


# ---------------------------------------------------------------- sources

def label_names(labels) -> list[str]:
    out = []
    for lab in labels or []:
        name = lab.get("name") if isinstance(lab, dict) else lab
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def normalize(raw: dict) -> dict:
    num = raw.get("number", raw.get("id"))
    if isinstance(num, str):
        num = num.lstrip("#")
    try:
        num = int(num)
    except (TypeError, ValueError) as exc:
        raise InputError(f"issue without a numeric number: {raw!r:.80}") from exc
    state = str(raw.get("state") or "open").lower()
    return {
        "number": num,
        "title": str(raw.get("title") or "").strip(),
        "body": str(raw.get("body") or raw.get("description") or ""),
        "labels": label_names(raw.get("labels")),
        "updatedAt": raw.get("updatedAt") or raw.get("updated_at") or raw.get("updated"),
        "state": "closed" if state in ("closed", "done", "merged", "completed") else "open",
    }


def from_json_text(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("issues")
    if not isinstance(data, list):
        raise InputError('expected a JSON list of issues or {"issues": [...]}')
    return [normalize(item) for item in data if isinstance(item, dict)]


def from_markdown(text: str) -> list[dict]:
    """Parse '- #12 Title (labels: a, b) (updated: 2026-01-01)' items; indented lines are body."""
    issues: list[dict] = []
    for line in text.splitlines():
        m = MD_ITEM_RE.match(line)
        if m:
            rest = m.group(3)
            labels = []
            lm = MD_LABELS_RE.search(rest)
            if lm:
                labels = [x.strip() for x in lm.group(1).split(",") if x.strip()]
                rest = rest.replace(lm.group(0), "")
            um = MD_UPDATED_RE.search(rest)
            updated = None
            if um:
                updated = um.group(1)
                rest = rest.replace(um.group(0), "")
            title = re.sub(r"\(\s*\)", "", DEP_RE.sub("", rest)).strip(" -:()")
            issues.append({"number": int(m.group(2)), "title": re.sub(r"\s+", " ", title),
                           "body": m.group(3), "labels": labels, "updatedAt": updated,
                           "state": "closed" if (m.group(1) or "").lower() == "x" else "open"})
        elif issues and line.startswith((" ", "\t")) and line.strip():
            issues[-1]["body"] += "\n" + line.strip()
    if not issues:
        raise InputError("no '- #N title' items found in markdown")
    return issues


def from_file(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise InputError(f"file not found: {path}")
    text = p.read_text(encoding="utf-8")
    if text.lstrip().startswith(("[", "{")):
        return from_json_text(text)
    return from_markdown(text)


def from_github(repo: str | None, limit: int, runner=subprocess.run, which=shutil.which) -> list[dict]:
    if not which("gh"):
        raise InputError("skipped: gh not found")
    cmd = ["gh", "issue", "list", "--state", "open", "--limit", str(limit),
           "--json", "number,title,body,labels,updatedAt,state"]
    if repo:
        cmd += ["--repo", repo]
    proc = runner(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise InputError(f"gh failed: {(proc.stderr or proc.stdout).strip()}")
    return from_json_text(proc.stdout)


def gh_state_lookup(repo: str | None, runner=subprocess.run):
    """Return a function number -> 'closed' | 'open' | None using gh issue view."""
    def lookup(number: int):
        cmd = ["gh", "issue", "view", str(number), "--json", "number,state"]
        if repo:
            cmd += ["--repo", repo]
        proc = runner(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        try:
            state = str(json.loads(proc.stdout).get("state", "")).lower()
        except (json.JSONDecodeError, AttributeError):
            return None
        return "closed" if state in ("closed", "merged") else "open" if state == "open" else None
    return lookup


# ---------------------------------------------------------------- analysis

def strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


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


def body_refs(body: str) -> list[str]:
    """Dependency refs in a body, in the order check-issue-readiness finds them.

    Outside a Related section, a ref counts only after an explicit verb (depends on,
    blocked by, blocked on, requires). Inside a Dependencies section, a line that
    starts with a ref counts too, unless it says the ref is only related.
    """
    refs: list[str] = []
    for s in split_sections(strip_code(body)):
        if s["kind"] == "related":
            continue
        for m in DEP_RE.finditer("\n".join([s["heading"]] + s["lines"])):
            for token in re.split(r"\s*(?:,|&|\band\b)\s*", m.group(2), flags=re.IGNORECASE):
                if re.fullmatch(REF, token) and token not in refs:
                    refs.append(token)
        if s["kind"] != "dependencies":
            continue
        for line in s["lines"]:
            m = ITEM_RE.match(line)
            text = (m.group(1) if m else line).strip()
            bare = BARE_REF_RE.match(text)
            if bare and not NOT_DEP_RE.search(text):
                for ref in re.findall(REF, bare.group(1)):
                    if ref not in refs:
                        refs.append(ref)
    return refs


def parse_deps(issue: dict) -> tuple[list[int], list[str]]:
    """Return (local issue numbers, external refs) this issue depends on."""
    local: list[int] = []
    external: list[str] = []
    for token in body_refs(issue["body"]):
        m = REF_RE.fullmatch(token)
        if not m:
            continue  # tracker key such as ABC-12: not an issue in this list
        repo, num = m.groups()
        if repo:
            if token not in external:
                external.append(token)
        elif int(num) not in local:
            local.append(int(num))
    for lab in issue["labels"]:
        lm = DEP_LABEL_RE.match(lab)
        if lm and int(lm.group(1)) not in local:
            local.append(int(lm.group(1)))
    return local, external


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def find_cycles(nodes: list[int], graph: dict[int, list[int]]) -> list[list[int]]:
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    out: list[list[int]] = []
    counter = [0]

    def visit(v: int) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, []):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph.get(v, []):
                out.append(sorted(comp))

    for v in nodes:
        if v not in index:
            visit(v)
    return sorted(out)


def analyze(issues: list[dict], *, stale_days: int = 30, now: datetime | None = None,
            priority_labels: list[str] | None = None, blocked_labels: list[str] | None = None,
            lookup=None) -> dict:
    now = now or datetime.now(timezone.utc)
    priority = [p.lower() for p in (priority_labels or [])]
    blocked_set = {b.lower() for b in (blocked_labels if blocked_labels is not None else ["blocked"])}

    closed = {i["number"] for i in issues if i["state"] == "closed"}
    queue = {}
    for i in issues:
        if i["state"] == "open":
            queue.setdefault(i["number"], i)
    nodes = sorted(queue)

    graph: dict[int, list[int]] = {}
    missing: list[dict] = []
    reasons: dict[int, list[str]] = {n: [] for n in nodes}
    lookup_cache: dict[int, str | None] = {}
    for n in nodes:
        local, external = parse_deps(queue[n])
        edges = []
        for d in local:
            if d in queue:
                edges.append(d)
                continue
            if d in closed:
                status = "closed"
            else:
                if lookup and d not in lookup_cache:
                    lookup_cache[d] = lookup(d)
                found = lookup_cache.get(d)
                status = "closed" if found == "closed" else "open-not-listed" if found == "open" \
                    else "unknown"
            missing.append({"issue": n, "dependency": f"#{d}", "status": status})
            if status == "open-not-listed":
                reasons[n].append(f"depends on open #{d}, which is not in this list")
            elif status == "unknown":
                reasons[n].append(f"depends on unknown #{d}")
        for ref in external:
            missing.append({"issue": n, "dependency": ref, "status": "external"})
        graph[n] = edges
        for lab in queue[n]["labels"]:
            if lab.lower() in blocked_set:
                reasons[n].append(f'label "{lab}"')

    cycles = find_cycles(nodes, graph)
    for cyc in cycles:
        path = " -> ".join(f"#{c}" for c in cyc + [cyc[0]])
        for c in cyc:
            reasons[c].append(f"in dependency cycle {path}")

    # propagate: anything depending on a blocked item is blocked too
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if reasons[n]:
                continue
            for d in graph[n]:
                if reasons[d]:
                    reasons[n].append(f"depends on blocked #{d}")
                    changed = True
                    break

    def rank(n: int) -> tuple[int, int]:
        labs = [lab.lower() for lab in queue[n]["labels"]]
        for idx, p in enumerate(priority):
            if p in labs:
                return idx, n
        return len(priority), n

    runnable = [n for n in nodes if not reasons[n]]
    done: set[int] = set()
    layers: list[list[int]] = []
    while True:
        ready = [n for n in runnable if n not in done and all(d in done for d in graph[n])]
        if not ready:
            break
        ready.sort(key=rank)
        layers.append(ready)
        done.update(ready)

    stale = []
    for n in nodes:
        t = parse_time(queue[n].get("updatedAt"))
        if t and now - t > timedelta(days=stale_days):
            stale.append({"issue": n, "days": (now - t).days})

    def brief(n: int) -> dict:
        return {"number": n, "title": queue[n]["title"]}

    return {
        "layers": [[brief(n) for n in layer] for layer in layers],
        "order": [n for layer in layers for n in layer],
        "blocked": [dict(brief(n), reasons=reasons[n]) for n in nodes if reasons[n]],
        "cycles": cycles,
        "missing": missing,
        "stale": stale,
        "stale_days": stale_days,
        "counts": {"open": len(nodes), "runnable": len(runnable),
                   "blocked": len(nodes) - len(runnable), "groups": len(layers)},
    }


def format_human(r: dict) -> str:
    c = r["counts"]
    lines = [f"{c['open']} open, {c['runnable']} runnable in {c['groups']} groups, "
             f"{c['blocked']} blocked"]
    if r["layers"]:
        lines.append("run order (issues in one group can run in parallel):")
        for i, layer in enumerate(r["layers"], 1):
            lines.append(f"  {i}. " + ", ".join(f"#{x['number']} {x['title']}".strip() for x in layer))
    if r["blocked"]:
        lines.append("blocked:")
        for b in r["blocked"]:
            lines.append(f"  #{b['number']} {b['title']}: {'; '.join(b['reasons'])}")
    for cyc in r["cycles"]:
        lines.append("cycle: " + " -> ".join(f"#{x}" for x in cyc + [cyc[0]]))
    for m in r["missing"]:
        if m["status"] == "closed":
            lines.append(f"closed dependency: #{m['issue']} -> {m['dependency']} (satisfied; "
                         "remove the reference)")
        elif m["status"] == "external":
            lines.append(f"external dependency: #{m['issue']} -> {m['dependency']} (not checked)")
    if r["stale"]:
        lines.append(f"stale (no update in >{r['stale_days']}d): "
                     + ", ".join(f"#{s['issue']} ({s['days']}d)" for s in r["stale"]))
    return "\n".join(lines)


def split_csv(values: list[str] | None) -> list[str]:
    out = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


def main(argv: list[str] | None = None, runner=subprocess.run, which=shutil.which) -> int:
    ap = argparse.ArgumentParser(
        description="Order open issues into parallel run groups from their dependencies.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--gh", action="store_true", help="read open issues with gh issue list")
    src.add_argument("--file", metavar="PATH", help="JSON list of issues or markdown '- #N title' list")
    ap.add_argument("--repo", help="owner/name for --gh (default: current repo)")
    ap.add_argument("--limit", type=int, default=200, help="max issues to read with --gh (default 200)")
    ap.add_argument("--no-resolve", action="store_true",
                    help="with --gh, do not look up the state of dependencies outside the list")
    ap.add_argument("--stale-days", type=int, default=30, help="flag items not updated in N days (default 30)")
    ap.add_argument("--priority-label", action="append", metavar="LABELS",
                    help="comma list of labels, highest first, to order issues within a group")
    ap.add_argument("--blocked-label", action="append", metavar="LABELS",
                    help='labels that mark an issue blocked (default "blocked")')
    ap.add_argument("--now", help="ISO timestamp to use as now (for reproducible output)")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = ap.parse_args(argv)

    try:
        now = parse_time(args.now) if args.now else None
        if args.now and now is None:
            raise InputError(f"bad --now timestamp: {args.now}")
        lookup = None
        if args.gh:
            issues = from_github(args.repo, args.limit, runner=runner, which=which)
            if not args.no_resolve:
                lookup = gh_state_lookup(args.repo, runner=runner)
        else:
            issues = from_file(args.file)
    except InputError as exc:
        msg = str(exc)
        if args.json:
            key = "skipped" if msg.startswith("skipped:") else "input_error"
            print(json.dumps({key: msg.removeprefix("skipped: ")}))
        else:
            print(msg if msg.startswith("skipped:") else f"input error: {msg}", file=sys.stderr)
        return 2

    blocked_labels = split_csv(args.blocked_label) if args.blocked_label else None
    result = analyze(issues, stale_days=args.stale_days, now=now,
                     priority_labels=split_csv(args.priority_label),
                     blocked_labels=blocked_labels, lookup=lookup)
    print(json.dumps(result, indent=2) if args.json else format_human(result))
    broken = result["cycles"] or any(m["status"] == "unknown" for m in result["missing"])
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
