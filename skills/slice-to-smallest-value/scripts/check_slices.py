#!/usr/bin/env python3
"""Check a slice document against the slicing rules.

Input: a JSON file, or a markdown file containing a fenced ```json block
with the slice document. See SKILL.md for the schema.

Exit codes: 0 pass, 1 rule violations, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

MAX_SCOPE = 5
MAX_CRITERIA = 5

VAGUE_PROOFS = {
    "", "-", "n/a", "na", "none", "tbd", "todo", "tba", "?", "works", "it works",
    "test", "tests", "a test", "unit test", "unit tests", "check", "checks",
    "verified", "verify", "qa", "looks good", "lgtm", "see above", "obvious",
    "e2e", "integration test", "ci", "ci passes", "passes", "done",
}
VAGUE_PREFIXES = ("manual", "by hand", "eyeball", "visual check", "visually")
COMMAND_WORDS = {
    "npm", "pnpm", "yarn", "bun", "npx", "node", "dotnet", "pytest", "python",
    "python3", "go", "cargo", "make", "just", "mvn", "gradle", "jest", "vitest",
    "playwright", "cypress", "curl", "gh", "bash", "sh", "deno", "rspec", "phpunit",
}
CONCRETE_RE = re.compile(
    r"`[^`]+`"                      # backticked command or identifier
    r"|\"[^\"]{3,}\"|'[^']{3,}'"    # quoted test name
    r"|\w+(?:[._/:]|::)\w+"         # dotted, pathed or namespaced identifier
    r"|\btest_\w+|\w+Test\b|\w+Tests\b|\w+_spec\b"
)
_LAYER = (r"(?:backend|frontend|front-end|back-end|database|db|schema|migrations?|models?|"
          r"api|ui|endpoints?|data|service|infrastructure|infra|scaffold(?:ing)?|"
          r"plumbing|refactor(?:ing)?|tests?)")
HORIZONTAL_RE = re.compile(
    rf"^(?:add |create |build |set ?up |write |all )?(?:the )?"
    rf"{_LAYER}(?:\s+(?:{_LAYER}|only|layer|changes|work))*$",
    re.IGNORECASE,
)


class InputError(Exception):
    pass


# ---------------------------------------------------------------- loading

def extract_json_text(text: str) -> str:
    """Return the JSON text from raw JSON or the first fenced json block holding slices."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return text
    blocks = re.findall(r"```(?:json|JSON)[^\n]*\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        if '"slices"' in block:
            return block
    if blocks:
        return blocks[0]
    raise InputError("no JSON object and no fenced ```json block found")


def load_doc(path: str) -> dict:
    if path == "-":
        text = sys.stdin.read()
    else:
        p = Path(path)
        if not p.is_file():
            raise InputError(f"file not found: {path}")
        text = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(extract_json_text(text))
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise InputError("top level must be a JSON object")
    if not isinstance(doc.get("slices"), list):
        raise InputError('missing "slices" list')
    if not isinstance(doc.get("parent", {}), dict):
        raise InputError('"parent" must be an object')
    return doc


# ---------------------------------------------------------------- helpers

def as_list(value) -> list:
    return value if isinstance(value, list) else []


def proof_problem(proof) -> str | None:
    """Return why a proof is not acceptable, or None if it names a test or check."""
    if not isinstance(proof, str) or not proof.strip():
        return "proof is empty"
    p = proof.strip()
    low = p.lower().rstrip(".")
    if low in VAGUE_PROOFS:
        return f'proof "{p}" does not name a test or check'
    if low.startswith(VAGUE_PREFIXES):
        return f'proof "{p}" is manual; name an automated test, check, or command'
    body = re.sub(r"^(test|check|cmd|command|proof)\s*:\s*", "", p, flags=re.IGNORECASE)
    if CONCRETE_RE.search(body):
        return None
    words = re.findall(r"[A-Za-z0-9_-]+", body.lower())
    if words and words[0] in COMMAND_WORDS:
        return None
    return f'proof "{p}" does not name a test or check (use a test name, file::test, or a `command`)'


def files_overlap(a: str, b: str) -> bool:
    a, b = a.strip().removeprefix("./"), b.strip().removeprefix("./")
    if not a or not b:
        return False
    if a == b:
        return True
    if any(ch in a for ch in "*?[") and fnmatch.fnmatch(b, a):
        return True
    if any(ch in b for ch in "*?[") and fnmatch.fnmatch(a, b):
        return True
    if a.endswith("/") and b.startswith(a):
        return True
    if b.endswith("/") and a.startswith(b):
        return True
    return False


def reachable(graph: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = list(graph.get(start, []))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return seen


def find_cycles(ids: list[str], graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan SCC; return components that form cycles, in input order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
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
                out.append(sorted(comp, key=ids.index))

    for v in ids:
        if v not in index:
            visit(v)
    return out


def layers(ids: list[str], graph: dict[str, list[str]]) -> list[list[str]]:
    """Kahn layers; graph maps node -> nodes it depends on. Cyclic nodes are left out."""
    remaining = {i: {d for d in graph.get(i, []) if d in ids} for i in ids}
    done: set[str] = set()
    out: list[list[str]] = []
    while True:
        ready = [i for i in ids if i not in done and remaining[i] <= done]
        if not ready:
            break
        out.append(ready)
        done.update(ready)
    return out


# ---------------------------------------------------------------- checking

def check(doc: dict) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(code, message, slice_id=None):
        errors.append({"code": code, "slice": slice_id, "message": message})

    def warn(code, message, slice_id=None):
        warnings.append({"code": code, "slice": slice_id, "message": message})

    parent = doc.get("parent") or {}
    parent_ids: list[str] = []
    for c in as_list(parent.get("criteria")):
        cid = c.get("id") if isinstance(c, dict) else None
        if not cid:
            err("parent-criterion-id", "parent criterion without an id")
            continue
        if cid in parent_ids:
            err("duplicate-id", f"duplicate parent criterion id {cid}")
        parent_ids.append(cid)
    if not parent_ids:
        warn("no-parent-criteria", "parent has no criteria; coverage cannot be checked")

    slices = [s for s in doc["slices"] if isinstance(s, dict)]
    if len(slices) != len(doc["slices"]):
        err("schema", "every entry in slices must be an object")
    if not slices:
        err("no-slices", "no slices defined")

    ids: list[str] = []
    for n, s in enumerate(slices, 1):
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            err("schema", f"slice #{n} has no id")
            s["id"] = sid = f"#{n}"
        if sid in ids:
            err("duplicate-id", f"duplicate slice id {sid}", sid)
        ids.append(sid)

    graph: dict[str, list[str]] = {}
    covered: set[str] = set()
    for s in slices:
        sid = s["id"]
        title = str(s.get("title") or "").strip()
        if not title:
            err("schema", "slice has no title", sid)
        if not str(s.get("value") or "").strip():
            err("no-value", "slice has no user-visible value statement", sid)
        if title and HORIZONTAL_RE.match(title):
            warn("horizontal", f'title "{title}" names a layer, not user value; cut vertically', sid)

        scope = as_list(s.get("scope"))
        if not scope:
            warn("no-scope", "slice has no scope items", sid)
        if len(scope) > MAX_SCOPE:
            err("too-much-scope", f"{len(scope)} scope items (max {MAX_SCOPE}); split the slice", sid)

        criteria = [c for c in as_list(s.get("criteria")) if isinstance(c, dict)]
        if not criteria:
            err("no-criteria", "slice has no acceptance criteria", sid)
        if len(criteria) > MAX_CRITERIA:
            err("too-many-criteria",
                f"{len(criteria)} criteria (max {MAX_CRITERIA}); split the slice", sid)
        seen_c: set[str] = set()
        slice_covers: set[str] = set()
        for c in criteria:
            cid = c.get("id") or "?"
            if cid in seen_c:
                err("duplicate-id", f"duplicate criterion id {cid}", sid)
            seen_c.add(cid)
            if not str(c.get("text") or "").strip():
                err("schema", f"criterion {cid} has no text", sid)
            problem = proof_problem(c.get("proof"))
            if problem:
                err("weak-proof", f"criterion {cid}: {problem}", sid)
            for pc in as_list(c.get("covers")):
                if pc not in parent_ids:
                    err("unknown-parent-criterion",
                        f"criterion {cid} covers unknown parent criterion {pc}", sid)
                else:
                    slice_covers.add(pc)
        if parent_ids and criteria and not slice_covers:
            warn("covers-nothing",
                 "slice covers no parent criterion; is it user-visible value or setup to fold in?",
                 sid)
        covered |= slice_covers

        deps = []
        for d in as_list(s.get("depends_on")):
            if d == sid:
                err("self-dependency", "slice depends on itself", sid)
            elif d not in ids:
                err("unknown-dependency", f"depends on unknown slice {d}", sid)
            else:
                deps.append(d)
        graph[sid] = deps

    for pc in parent_ids:
        if pc not in covered:
            err("uncovered", f"parent criterion {pc} is not covered by any slice")

    cycles = find_cycles(ids, graph)
    for cyc in cycles:
        err("cycle", "dependency cycle: " + " -> ".join(cyc + [cyc[0]]))

    reach = {sid: reachable(graph, sid) for sid in ids}
    overlaps = []
    for i, a in enumerate(slices):
        for b in slices[i + 1:]:
            if b["id"] in reach[a["id"]] or a["id"] in reach[b["id"]]:
                continue
            shared = sorted({fa for fa in as_list(a.get("files")) for fb in as_list(b.get("files"))
                             if isinstance(fa, str) and isinstance(fb, str) and files_overlap(fa, fb)})
            if shared:
                overlaps.append({"slices": [a["id"], b["id"]], "files": shared})

    order_layers = layers(ids, graph)
    return {
        "errors": errors,
        "warnings": warnings,
        "overlaps": overlaps,
        "cycles": cycles,
        "layers": order_layers,
        "order": [sid for layer in order_layers for sid in layer],
        "slice_count": len(slices),
    }


def apply_overlap_policy(result: dict, policy: str) -> None:
    for ov in result["overlaps"]:
        a, b = ov["slices"]
        msg = (f"{a} and {b} are independent but both touch {', '.join(ov['files'])}; "
               f"add a dependency to serialize them or merge them")
        entry = {"code": "file-overlap", "slice": a, "message": msg}
        (result["errors"] if policy == "fail" else result["warnings"]).append(entry)
    result["ok"] = not result["errors"]


def format_human(result: dict) -> str:
    lines = []
    status = "PASS" if result["ok"] else "FAIL"
    lines.append(f"{status}: {result['slice_count']} slices, "
                 f"{len(result['errors'])} errors, {len(result['warnings'])} warnings")
    for e in result["errors"]:
        where = f"[{e['slice']}] " if e["slice"] else ""
        lines.append(f"  error   {where}{e['message']}")
    for w in result["warnings"]:
        where = f"[{w['slice']}] " if w["slice"] else ""
        lines.append(f"  warning {where}{w['message']}")
    if result["layers"]:
        lines.append("order (slices on one line can run in parallel):")
        for n, layer in enumerate(result["layers"], 1):
            lines.append(f"  {n}. {', '.join(layer)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check slices: size limits, proofs, coverage, dependency graph, file overlap.")
    ap.add_argument("path", help="slice JSON file, markdown file with a ```json block, or - for stdin")
    ap.add_argument("--overlap", choices=["fail", "warn"], default="fail",
                    help="how to treat files shared by independent slices (default: fail)")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = ap.parse_args(argv)

    try:
        doc = load_doc(args.path)
    except InputError as exc:
        if args.json:
            print(json.dumps({"ok": False, "input_error": str(exc)}))
        else:
            print(f"input error: {exc}", file=sys.stderr)
        return 2

    result = check(doc)
    apply_overlap_policy(result, args.overlap)
    if args.strict and result["warnings"]:
        result["ok"] = False
    print(json.dumps(result, indent=2) if args.json else format_human(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
