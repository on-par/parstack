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
MAX_FILES = 10      # more files than this is a warning, not an error
MAX_ITEM_CHARS = 200  # a scope item or criterion longer than this is probably several

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
BACKTICK_RE = re.compile(r"`([^`]+)`")
QUOTED_RE = re.compile(r"\"[^\"]{3,}\"|'[^']{3,}'")   # quoted test name
NAMESPACED_RE = re.compile(r"\w(?:[\w./-]*\w)?::\w")      # file::test or pkg::Test
TEST_NAME_RE = re.compile(r"\btest_\w+|\b\w+Tests?\b|\bTest[A-Z]\w*|\b\w+_spec\b")
PATH_RE = re.compile(r"[\w.-]+(?:/[\w.-]+)+/?|[\w-]+\.[A-Za-z]\w*")
TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?|e2e)/"          # under a test directory
    r"|(?:^|/)test_[\w-]+\.\w+$"                         # test_reset.py
    r"|[\w-]_(?:test|spec)\.\w+$"                         # reset_test.go, reset_spec.rb
    r"|\.(?:test|spec)\.\w+$"                              # reset.test.ts, reset.spec.ts
    r"|_spec\b"
)
PROSE_RE = re.compile(
    r"\b(?:PR|pull request)(?:'s)?\s+(?:body|description|text|notes?|comments?|records?|"
    r"lists?|mentions?|says|includes?|documents?|explains?|shows?)\b",
    re.IGNORECASE,
)
EXTERNAL_DEP_RE = re.compile(
    r"^(?:#\d+"                                # #40
    r"|[\w.-]+/[\w.-]+#\d+"                   # owner/repo#7
    r"|[A-Z][A-Z0-9]+-\d+)$"                   # PROJ-12
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


def is_test_path(token: str) -> bool:
    return bool(TEST_PATH_RE.search(token.strip().removeprefix("./")))


def is_command(text: str) -> bool:
    words = re.findall(r"[A-Za-z0-9_./-]+", text.lower())
    return bool(words) and words[0] in COMMAND_WORDS


def names_test(text: str) -> bool:
    """True when text names a test: file::test, a test name, or a test file path."""
    if NAMESPACED_RE.search(text) or TEST_NAME_RE.search(text):
        return True
    return any(is_test_path(m.group(0)) for m in PATH_RE.finditer(text))


def proof_problem(proof) -> str | None:
    """Return why a proof is not acceptable, or None if it names a test or command."""
    if not isinstance(proof, str) or not proof.strip():
        return "proof is empty"
    p = proof.strip()
    low = p.lower().rstrip(".")
    if low in VAGUE_PROOFS:
        return f'proof "{p}" does not name a test or check'
    if low.startswith(VAGUE_PREFIXES):
        return f'proof "{p}" is manual; name an automated test, check, or command'
    body = re.sub(r"^(test|check|cmd|command|proof)\s*:\s*", "", p, flags=re.IGNORECASE)
    for m in BACKTICK_RE.finditer(body):
        code = m.group(1).strip()
        # a backticked command (has arguments or starts with a known tool) or test name
        if " " in code or is_command(code) or names_test(code):
            return None
    plain = BACKTICK_RE.sub(" ", body)
    if names_test(plain) or QUOTED_RE.search(plain) or is_command(plain):
        return None
    if PATH_RE.search(body):
        return (f'proof "{p}" names a file that is not a test; name a test file '
                f'(*.test.*, *_test.*, test_*, tests/...), file::test, or a `command`')
    return f'proof "{p}" does not name a test or check (use a test name, file::test, or a `command`)'


def packed_problem(text: str) -> str | None:
    """Return why one scope item or criterion looks like several, or None."""
    if len(text) > MAX_ITEM_CHARS:
        return f"is {len(text)} chars (over {MAX_ITEM_CHARS})"
    if ";" in text:
        return 'joins several changes with ";"'
    return None


def directory_of(path: str) -> str:
    path = path.strip().removeprefix("./")
    if path.endswith("/"):
        return path.rstrip("/") or "."
    head = path.rsplit("/", 1)[0] if "/" in path else "."
    return head or "."


def is_shared(path: str, shared: list[str]) -> bool:
    path = path.strip().removeprefix("./")
    for sh in shared:
        sh = sh.strip().removeprefix("./")
        if not sh:
            continue
        if path == sh:
            return True
        if any(ch in sh for ch in "*?[") and fnmatch.fnmatch(path, sh):
            return True
        if sh.endswith("/") and path.startswith(sh):
            return True
    return False


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

    dropped = []
    raw_dropped = parent.get("dropped", [])
    if not isinstance(raw_dropped, list):
        err("schema", '"parent.dropped" must be a list')
        raw_dropped = []
    for n, d in enumerate(raw_dropped, 1):
        if not isinstance(d, dict) or not str(d.get("text") or "").strip():
            err("schema", f"dropped idea #{n} has no text")
            continue
        entry = {"text": str(d["text"]).strip(), "reason": str(d.get("reason") or "").strip(),
                 "covered_by": str(d.get("covered_by") or "").strip()}
        if not entry["reason"]:
            err("dropped-without-reason", f'dropped idea "{entry["text"]}" has no reason')
        dropped.append(entry)

    shared_files = doc.get("shared_files", [])
    if not isinstance(shared_files, list) or not all(isinstance(f, str) for f in shared_files):
        err("schema", '"shared_files" must be a list of paths')
        shared_files = []

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
    external: dict[str, list[str]] = {}
    sizes: dict[str, dict] = {}
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
        for n, item in enumerate(scope, 1):
            problem = packed_problem(str(item))
            if problem:
                warn("packed-item", f"scope item {n} {problem}; split it into separate items or slices",
                     sid)

        if "context" in s and not isinstance(s["context"], str):
            err("schema", '"context" must be a string', sid)
        out_of_scope = s.get("out_of_scope", [])
        if not isinstance(out_of_scope, list):
            err("schema", '"out_of_scope" must be a list', sid)
        elif not [x for x in out_of_scope if str(x).strip()]:
            warn("no-out-of-scope",
                 "slice lists no out_of_scope items; name what it will not do", sid)

        files = [f for f in as_list(s.get("files")) if isinstance(f, str) and f.strip()]
        dirs = sorted({directory_of(f) for f in files})
        sizes[sid] = {"files": len(files), "directories": len(dirs)}
        if len(files) > MAX_FILES:
            warn("many-files",
                 f"touches {len(files)} files across {len(dirs)} directories "
                 f"(over {MAX_FILES}); consider splitting", sid)

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
            ctext = str(c.get("text") or "").strip()
            if not ctext:
                err("schema", f"criterion {cid} has no text", sid)
            packed = packed_problem(ctext)
            if packed:
                warn("packed-item", f"criterion {cid} {packed}; split it into separate criteria", sid)
            prose = PROSE_RE.search(ctext)
            if prose:
                warn("prose-criterion",
                     f'criterion {cid} is about PR prose ("{prose.group(0)}"), which no test can '
                     f"prove; make it a check or move it to the PR description", sid)
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
            elif d in ids:
                deps.append(d)
            elif isinstance(d, str) and EXTERNAL_DEP_RE.match(d.strip()):
                external.setdefault(sid, []).append(d.strip())
            else:
                err("unknown-dependency",
                    f"depends on unknown slice {d} (external issues look like #12, "
                    f"owner/repo#12, or ABC-12)", sid)
        graph[sid] = deps

    for pc in parent_ids:
        if pc not in covered:
            err("uncovered", f"parent criterion {pc} is not covered by any slice; cover it, "
                f"or move it to parent.dropped with a reason")

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
                             if isinstance(fa, str) and isinstance(fb, str) and files_overlap(fa, fb)
                             and not (is_shared(fa, shared_files) and is_shared(fb, shared_files))})
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
        "external_dependencies": external,
        "sizes": sizes,
        "dropped": dropped,
        "slice_count": len(slices),
    }


def apply_overlap_policy(result: dict, policy: str) -> None:
    for ov in result["overlaps"]:
        a, b = ov["slices"]
        msg = (f"{a} and {b} are independent but both touch {', '.join(ov['files'])}; "
               f"add a dependency to serialize them, merge them, or list an append-only "
               f"file (e.g. CHANGELOG.md) in shared_files")
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
    if result["sizes"]:
        lines.append("size:")
        for sid, size in result["sizes"].items():
            nf, nd = size["files"], size["directories"]
            lines.append(f"  {sid}: touches {nf} file{'s' if nf != 1 else ''} across "
                         f"{nd} director{'ies' if nd != 1 else 'y'}")
    if result["dropped"]:
        lines.append("dropped from the parent (no slice):")
        for d in result["dropped"]:
            extra = f" (covered by {d['covered_by']})" if d["covered_by"] else ""
            lines.append(f"  - {d['text']}: {d['reason'] or 'no reason given'}{extra}")
    if result["layers"]:
        ext = result["external_dependencies"]
        lines.append("order (slices on one line can run in parallel):")
        for n, layer in enumerate(result["layers"], 1):
            lines.append(f"  {n}. {', '.join(layer)}")
            for sid in layer:
                if ext.get(sid):
                    lines.append(f"     {sid} waits on external {', '.join(ext[sid])}")
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
