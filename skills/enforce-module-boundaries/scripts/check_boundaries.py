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


def run_depcruise(repo: Path, config: str, sources: list[str], timeout: float, env: Env) -> dict | None:
    """The raw JSON report, or None when dependency-cruiser is not available."""
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
    return data


# ---------------------------------------------------------------- unresolved path aliases

ALIAS_PREFIXES = ("@/", "~/")
TSCONFIG_RE = re.compile(r"""tsConfig\s*:\s*\{[^}]*?fileName\s*:\s*["']([^"']+)["']""")


def strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments and trailing commas outside strings (tsconfig is JSONC)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def tsconfig_paths(path: Path, depth: int = 0) -> dict:
    """compilerOptions.paths from a tsconfig, following relative `extends` (closest wins)."""
    try:
        data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    paths = {}
    parents = data.get("extends")
    parents = [parents] if isinstance(parents, str) else parents if isinstance(parents, list) else []
    if depth < 5:
        for parent in parents:
            if isinstance(parent, str) and parent.startswith("."):
                target = (path.parent / parent).resolve()
                if target.suffix != ".json":
                    target = target.with_name(target.name + ".json")
                paths.update(tsconfig_paths(target, depth + 1))
    own = (data.get("compilerOptions") or {}).get("paths") if isinstance(data.get("compilerOptions"), dict) else None
    if isinstance(own, dict):
        paths.update(own)
    return paths


def alias_matchers(repo: Path, config: str | None) -> tuple[tuple[str, ...], set[str]]:
    """(prefixes, exact names) that look like path aliases: @/ and ~/ plus tsconfig paths keys."""
    tsconfig = "tsconfig.json"
    if config:
        try:
            match = TSCONFIG_RE.search((repo / config).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            match = None
        if match:
            tsconfig = match.group(1)
    prefixes, exact = set(ALIAS_PREFIXES), set()
    for name in tsconfig_paths(repo / tsconfig):
        if "*" in name:
            prefix = name.split("*", 1)[0]
            if prefix:  # a bare "*" key would match every package name
                prefixes.add(prefix)
        else:
            exact.add(name)
    return tuple(sorted(prefixes)), exact


def unresolved_aliases(data: dict, prefixes: tuple[str, ...], exact: set[str]) -> list[dict]:
    out = set()
    for mod in data.get("modules") or []:
        if not isinstance(mod, dict):
            continue
        for dep in mod.get("dependencies") or []:
            if not isinstance(dep, dict) or not dep.get("couldNotResolve"):
                continue
            name = str(dep.get("module", ""))
            if name.startswith(prefixes) or name in exact:
                out.add((str(mod.get("source", "")), name))
    return [{"from": f, "module": m} for f, m in sorted(out)]


def alias_warning(found: list[dict], config: str | None) -> str:
    sample = ", ".join(f"'{d['module']}' in {d['from']}" for d in found[:3])
    where = f"options.tsConfig in {config}" if config else "options.tsConfig in the dependency-cruiser config"
    return (f"{len(found)} unresolved import(s) look like path aliases ({sample}); dependency-cruiser is not "
            f"resolving them, so violations through aliases are missed. Check that {where} points at the "
            f"tsconfig that defines compilerOptions.paths.")


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
    warnings = []

    if stack == "ts":
        tool = "dependency-cruiser"
        config = args.config or next((c for c in DEPCRUISE_CONFIGS if (repo / c).exists()), None)
        if args.depcruise_json:
            try:
                data = json.loads(Path(args.depcruise_json).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"cannot read {args.depcruise_json}: {exc}")
            current = parse_depcruise(data)
        else:
            if not config:
                raise ValueError("no dependency-cruiser config found; run gen_boundaries.py --write or pass --config")
            sources = args.source or (["src"] if (repo / "src").is_dir() else ["."])
            data = run_depcruise(repo, config, sources, args.timeout, env)
            if data is None:
                result.update(status="skipped", reason="skipped: dependency-cruiser not found")
                return result
            current = parse_depcruise(data)
        found = unresolved_aliases(data, *alias_matchers(repo, config))
        if found:
            result["unresolved_aliases"] = found
            warnings.append(alias_warning(found, config))
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

    result["warnings"] = warnings
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
    for w in result.get("warnings", []):
        lines.append(f"  warn: {w}")
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
