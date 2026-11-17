#!/usr/bin/env python3
"""Run boundary rules and fail only on violations that are not in the baseline.

TypeScript/JavaScript: runs dependency-cruiser (local bin, PATH, or npx --no-install)
with JSON output, or reads a saved report via --depcruise-json.
.NET: runs `dotnet test --filter FullyQualifiedName~ModuleBoundariesTests` and parses
failed module pairs, or reads saved output via --dotnet-output.

Exit codes: 0 no new violations (or tool skipped, or baseline updated),
1 new violations or the rule run failed, 2 usage/input error.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

TEST_CLASS = "ModuleBoundariesTests"
FAILED_PAIR_RE = re.compile(
    r"Failed\s+\S*" + TEST_CLASS + r"\.\w+\(\s*(?:from:\s*)?\"([^\"]+)\",\s*(?:to:\s*)?\"([^\"]+)\"\s*\)")
DEPCRUISE_CONFIGS = (".dependency-cruiser.cjs", ".dependency-cruiser.js", ".dependency-cruiser.mjs",
                     ".dependency-cruiser.json")


class Env:
    """External calls, injectable for tests."""

    def __init__(self, which=shutil.which, run=subprocess.run):
        self.which = which
        self.run = run


class ToolError(Exception):
    pass


def key(v: dict) -> tuple[str, str, str]:
    return (v["rule"], v["from"], v["to"])


def normalize(violations: list[dict]) -> list[dict]:
    seen = {}
    for v in violations:
        seen[key(v)] = {"rule": v["rule"], "from": v["from"], "to": v["to"]}
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------- dependency-cruiser

def parse_depcruise(data: dict) -> list[dict]:
    summary = data.get("summary") if isinstance(data, dict) else None
    if not isinstance(summary, dict) or not isinstance(summary.get("violations"), list):
        raise ToolError("not a dependency-cruiser JSON report (missing summary.violations)")
    out = []
    for v in summary["violations"]:
        rule = v.get("rule") or {}
        if rule.get("severity") in ("info", "ignore"):
            continue
        out.append({"rule": rule.get("name", "unknown"), "from": v.get("from", ""), "to": v.get("to", "")})
    return normalize(out)


def depcruise_command(repo: Path, env: Env) -> tuple[list[str], bool] | None:
    """(argv prefix, via_npx) or None when dependency-cruiser is not available."""
    local = repo / "node_modules" / ".bin" / "depcruise"
    if local.exists():
        return [str(local)], False
    found = env.which("depcruise")
    if found:
        return [found], False
    if env.which("npx"):
        return ["npx", "--no-install", "depcruise"], True
    return None


def run_depcruise(repo: Path, config: str, sources: list[str], timeout: float, env: Env) -> list[dict] | None:
    cmd = depcruise_command(repo, env)
    if cmd is None:
        return None
    argv, via_npx = cmd
    proc = env.run([*argv, "--config", config, "--output-type", "json", *sources], cwd=str(repo),
                   capture_output=True, text=True, timeout=timeout)
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        if via_npx:
            return None  # npx could not find dependency-cruiser: treat as not installed
        raise ToolError(f"dependency-cruiser produced no JSON (exit {proc.returncode}): "
                        f"{(proc.stderr or '').strip()[-400:]}")
    return parse_depcruise(data)


# ---------------------------------------------------------------- .NET

def parse_dotnet(output: str) -> list[dict]:
    return normalize([{"rule": "module-boundary", "from": a, "to": b} for a, b in FAILED_PAIR_RE.findall(output)])


def dotnet_target(repo: Path) -> str | None:
    for pattern in ("*.sln", "*.slnx", "*.csproj"):
        hits = sorted(repo.glob(pattern))
        if hits:
            return hits[0].name
    return None


def run_dotnet(repo: Path, target: str | None, test_filter: str, timeout: float, env: Env) -> tuple[list[dict], str] | None:
    if not env.which("dotnet"):
        return None
    argv = ["dotnet", "test"] + ([target] if target else []) + ["--nologo", "--filter", test_filter]
    proc = env.run(argv, cwd=str(repo), capture_output=True, text=True, timeout=timeout)
    output = (proc.stdout or "") + (proc.stderr or "")
    violations = parse_dotnet(output)
    if proc.returncode != 0 and not violations:
        if "No test matches the given testcase filter" in output:
            raise ToolError(f"no tests match {test_filter}; generate them with gen_boundaries.py")
        raise ToolError(f"dotnet test failed without boundary failures (exit {proc.returncode}): "
                        f"{output.strip()[-400:]}")
    return violations, output


# ---------------------------------------------------------------- baseline

def load_baseline(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return normalize(data.get("violations", []))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"cannot read baseline {path}: {exc}")


def write_baseline(path: Path, tool: str, violations: list[dict]) -> None:
    data = {"tool": tool, "count": len(violations), "violations": normalize(violations)}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def compare(current: list[dict], baseline: list[dict]) -> tuple[list[dict], list[dict]]:
    cur = {key(v): v for v in current}
    base = {key(v): v for v in baseline}
    new = [cur[k] for k in sorted(cur.keys() - base.keys())]
    fixed = [base[k] for k in sorted(base.keys() - cur.keys())]
    return new, fixed


# ---------------------------------------------------------------- main

def detect_stack(repo: Path) -> str | None:
    if any((repo / c).exists() for c in DEPCRUISE_CONFIGS) or (repo / "package.json").exists():
        return "ts"
    if dotnet_target(repo) or list(repo.glob("**/*.csproj")):
        return "dotnet"
    return None


def check(repo: Path, args, env: Env) -> dict:
    stack = args.stack
    if stack == "auto":
        stack = "ts" if args.depcruise_json else "dotnet" if args.dotnet_output else detect_stack(repo)
    if not stack:
        raise ValueError("cannot detect stack; pass --stack")
    result = {"stack": stack, "baseline": args.baseline}

    if stack == "ts":
        tool = "dependency-cruiser"
        if args.depcruise_json:
            try:
                current = parse_depcruise(json.loads(Path(args.depcruise_json).read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot read {args.depcruise_json}: {exc}")
        else:
            config = args.config or next((c for c in DEPCRUISE_CONFIGS if (repo / c).exists()), None)
            if not config:
                raise ValueError("no dependency-cruiser config found; run gen_boundaries.py --write or pass --config")
            sources = args.source or (["src"] if (repo / "src").is_dir() else ["."])
            current = run_depcruise(repo, config, sources, args.timeout, env)
            if current is None:
                result.update(status="skipped", reason="skipped: dependency-cruiser not found")
                return result
    else:
        tool = "archunitnet"
        if args.dotnet_output:
            try:
                current = parse_dotnet(Path(args.dotnet_output).read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                raise ValueError(f"cannot read {args.dotnet_output}: {exc}")
        else:
            ran = run_dotnet(repo, args.dotnet_target or dotnet_target(repo), args.filter, args.timeout, env)
            if ran is None:
                result.update(status="skipped", reason="skipped: dotnet not found")
                return result
            current = ran[0]

    baseline_path = repo / args.baseline
    if args.update_baseline:
        write_baseline(baseline_path, tool, current)
        result.update(status="updated", current_count=len(current), new=[], fixed=[],
                      reason=f"baseline written with {len(current)} violation(s)")
        return result

    baseline = load_baseline(baseline_path)
    new, fixed = compare(current, baseline or [])
    result.update(current_count=len(current), baseline_count=len(baseline or []), new=new, fixed=fixed,
                  baseline_found=baseline is not None)
    if new:
        result.update(status="fail", reason=f"{len(new)} new violation(s) not in the baseline")
    else:
        result.update(status="pass", reason="no new violations")
    if fixed:
        result["reason"] += f"; {len(fixed)} baseline violation(s) fixed, run --update-baseline to lock that in"
    return result


def human(result: dict) -> str:
    lines = [f"boundaries ({result['stack']}): {result['status']} - {result['reason']}"]
    if "current_count" in result and result["status"] != "updated":
        lines.append(f"  current {result['current_count']}, baseline {result['baseline_count']}"
                     + ("" if result.get("baseline_found") else f" ({result['baseline']} not found)"))
    for v in result.get("new", []):
        lines.append(f"  NEW   [{v['rule']}] {v['from']} -> {v['to']}")
    for v in result.get("fixed", []):
        lines.append(f"  fixed [{v['rule']}] {v['from']} -> {v['to']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check module boundaries; fail only on violations not in the baseline.")
    parser.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    parser.add_argument("--stack", choices=["auto", "ts", "dotnet"], default="auto")
    parser.add_argument("--baseline", default="boundaries-baseline.json", help="baseline file, repo-relative")
    parser.add_argument("--update-baseline", action="store_true", help="write the current violations as the baseline")
    parser.add_argument("--config", help="dependency-cruiser config (default: .dependency-cruiser.cjs/.js/.mjs/.json)")
    parser.add_argument("--source", action="append", help="path to cruise (repeatable; default: src or .)")
    parser.add_argument("--depcruise-json", help="read a saved `depcruise --output-type json` report instead of running it")
    parser.add_argument("--dotnet-target", help="solution or project for dotnet test (default: first *.sln/*.csproj at root)")
    parser.add_argument("--filter", default=f"FullyQualifiedName~{TEST_CLASS}", help="dotnet test filter")
    parser.add_argument("--dotnet-output", help="read saved `dotnet test` console output instead of running it")
    parser.add_argument("--timeout", type=float, default=900, help="tool timeout, seconds")
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args(argv)

    repo = Path(args.path).resolve()
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 2
    try:
        result = check(repo, args, env or Env())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ToolError, subprocess.TimeoutExpired, OSError) as exc:
        result = {"stack": args.stack, "status": "error", "reason": str(exc), "baseline": args.baseline}
    print(json.dumps(result, indent=2) if args.json else human(result))
    return 1 if result["status"] in ("fail", "error") else 0


if __name__ == "__main__":
    sys.exit(main())
