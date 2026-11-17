#!/usr/bin/env python3
"""Render each slice as a markdown issue body, in dependency order.

Prints to stdout only. Nothing is posted anywhere.

Exit codes: 0 rendered, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_slices import InputError, as_list, layers, load_doc  # noqa: E402


def parse_issue_map(text: str | None) -> dict[str, str]:
    """Parse "S1=12,S2=13" into {"S1": "#12", "S2": "#13"}."""
    out: dict[str, str] = {}
    if not text:
        return out
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise InputError(f"bad --issue-map entry: {pair!r} (want ID=NUMBER)")
        key, num = (x.strip() for x in pair.split("=", 1))
        num = num.lstrip("#")
        if not num.isdigit():
            raise InputError(f"bad issue number for {key}: {num!r}")
        out[key] = f"#{num}"
    return out


def ordered(doc: dict) -> list[dict]:
    slices = [s for s in doc["slices"] if isinstance(s, dict) and s.get("id")]
    ids = [s["id"] for s in slices]
    graph = {s["id"]: [d for d in as_list(s.get("depends_on")) if d in ids] for s in slices}
    order = [sid for layer in layers(ids, graph) for sid in layer]
    order += [sid for sid in ids if sid not in order]  # cyclic leftovers keep input order
    by_id = {s["id"]: s for s in slices}
    return [by_id[sid] for sid in order]


def render_slice(s: dict, parent: dict, issue_map: dict[str, str]) -> str:
    parent_text = {c.get("id"): c.get("text", "") for c in as_list(parent.get("criteria"))
                   if isinstance(c, dict)}
    out = []
    out.append("## Value")
    out.append(str(s.get("value") or "").strip() or "_missing_")
    if parent.get("title"):
        pid = f" ({parent['id']})" if parent.get("id") else ""
        out.append(f"\nPart of: {parent['title']}{pid}")
    out.append("\n## Scope")
    out += [f"- {item}" for item in as_list(s.get("scope"))] or ["- _none listed_"]
    files = as_list(s.get("files"))
    if files:
        out.append("\n## Files")
        out += [f"- `{f}`" for f in files]
    out.append("\n## Acceptance criteria")
    for c in as_list(s.get("criteria")):
        if not isinstance(c, dict):
            continue
        out.append(f"- [ ] {c.get('text', '').strip()}")
        out.append(f"  - proof: {str(c.get('proof') or '').strip() or '_missing_'}")
        covers = [pc for pc in as_list(c.get("covers"))]
        if covers:
            labels = ", ".join(f"{pc} ({parent_text[pc]})" if parent_text.get(pc) else pc
                               for pc in covers)
            out.append(f"  - covers: {labels}")
    out.append("\n## Out of scope")
    out.append("- Anything not listed under Scope.")
    out.append("\n## Dependencies")
    deps = as_list(s.get("depends_on"))
    if deps:
        out += [f"- Depends on {issue_map.get(d, d)}" for d in deps]
    else:
        out.append("- None")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render slices as markdown issue bodies (stdout only, no posting).")
    ap.add_argument("path", help="slice JSON file, markdown file with a ```json block, or - for stdin")
    ap.add_argument("--issue-map", metavar="ID=N,...",
                    help='map slice ids to issue numbers so dependencies render as "#N"')
    ap.add_argument("--json", action="store_true",
                    help="print a JSON list of {id, title, body}")
    args = ap.parse_args(argv)

    try:
        doc = load_doc(args.path)
        issue_map = parse_issue_map(args.issue_map)
    except InputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2

    parent = doc.get("parent") or {}
    rendered = [{"id": s["id"], "title": str(s.get("title") or s["id"]),
                 "body": render_slice(s, parent, issue_map)} for s in ordered(doc)]
    if args.json:
        print(json.dumps(rendered, indent=2))
    else:
        chunks = [f"# {r['id']}: {r['title']}\n\n{r['body']}" for r in rendered]
        print("\n---\n\n".join(chunks), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
