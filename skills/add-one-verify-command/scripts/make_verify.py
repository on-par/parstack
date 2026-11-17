#!/usr/bin/env python3
"""Propose or write one `verify` entry point that chains the build, lint and test steps a repo has.

Node-only repos get a package.json "verify" script. Everything else (or mixed
stacks) gets scripts/verify.sh. Dry run by default; --write applies.

Exit codes: 0 proposed/written/already present, 1 nothing to chain, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_stack  # noqa: E402  (sibling module in this scripts dir)

STACKS = tuple(verify_stack.STEP_FUNCS)
SCRIPT_PATH = "scripts/verify.sh"


def render_verify_sh(steps: list[dict]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "# Single verify entry point: build, lint, test. Stops at the first failure.",
        "set -euo pipefail",
        'cd "$(dirname "$0")/.."',
        "",
        'step() { echo "==> $*"; "$@"; }',
        "",
    ]
    lines += [f"step {s['cmd']}" for s in steps]
    lines += ["", 'echo "verify: ok"', ""]
    return "\n".join(lines)


def json_indent(text: str) -> int | str:
    match = re.search(r'^\{\s*\n([ \t]+)"', text)
    if not match:
        return 2
    indent = match.group(1)
    return indent if "\t" in indent else len(indent)


def propose(root: Path, stacks: list[str] | None = None) -> dict:
    detected = verify_stack.detect_stacks(root)
    chosen = stacks or detected
    steps = []
    for name in chosen:
        steps += verify_stack.STEP_FUNCS[name](root)
    result = {"root": str(root), "detected_stacks": detected, "stacks": chosen, "steps": steps,
              "existing": verify_stack.existing_verify(root)}
    if not chosen or not steps:
        result.update(action="nothing", target=None, content=None,
                      reason="no supported stack detected" if not chosen else "no build, lint or test steps found")
        return result
    if chosen == ["node"]:
        command = " && ".join(s["cmd"] for s in steps)
        result.update(target="package.json", content=command,
                      verify_command=f"{verify_stack.package_manager(root)} run verify")
    else:
        result.update(target=SCRIPT_PATH, content=render_verify_sh(steps),
                      verify_command=f"bash {SCRIPT_PATH}")
    return result


def target_exists(root: Path, result: dict) -> bool:
    if result["target"] == "package.json":
        pkg = verify_stack.read_package_json(root) or {}
        return isinstance(pkg.get("scripts"), dict) and "verify" in pkg["scripts"]
    return (root / result["target"]).exists()


def write(root: Path, result: dict) -> None:
    if result["target"] == "package.json":
        path = root / "package.json"
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        scripts = data.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
            data["scripts"] = scripts
        scripts["verify"] = result["content"]
        path.write_text(json.dumps(data, indent=json_indent(text), ensure_ascii=False) + "\n", encoding="utf-8")
        return
    path = root / result["target"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result["content"], encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def human(result: dict) -> str:
    if result["action"] == "nothing":
        return f"nothing to write: {result['reason']}"
    lines = [f"stacks: {', '.join(result['stacks'])}", "steps:"]
    lines += [f"  {s['name']:<10} {s['cmd']}" for s in result["steps"]]
    verb = {"would-write": "would write", "wrote": "wrote", "exists": "already present, not changed (use --force)"}
    lines.append(f"{verb[result['action']]}: {result['target']}")
    if result["action"] != "exists":
        if result["target"] == "package.json":
            lines.append(f'  "verify": {json.dumps(result["content"])}')
        else:
            lines += ["  " + line if line else "" for line in result["content"].rstrip("\n").splitlines()]
    lines.append(f"run it with: {result.get('verify_command') or result['existing']}")
    if result["action"] == "would-write":
        lines.append("dry run: pass --write to apply")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Propose or write one verify entry point for a repo.")
    parser.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    parser.add_argument("--stack", action="append", choices=STACKS,
                        help="use this stack instead of detection (repeatable)")
    parser.add_argument("--write", action="store_true", help="write the verify entry point (default: dry run)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing verify entry point")
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    result = propose(root, args.stack)
    if result.get("action") != "nothing":
        if target_exists(root, result) and not args.force:
            result["action"] = "exists"
        elif args.write:
            write(root, result)
            result["action"] = "wrote"
        else:
            result["action"] = "would-write"
    print(json.dumps(result, indent=2) if args.json else human(result))
    return 1 if result["action"] == "nothing" else 0


if __name__ == "__main__":
    sys.exit(main())
