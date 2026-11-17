#!/usr/bin/env python3
"""Score a repository for agent readiness and print a ranked fix list.

Checks: single verify command, verify speed/flakiness (opt-in), boundary
rules, dead-code tooling, duplication tooling, secrets, agent context files,
CI config, tests. Score is 0-100 over checks that were not skipped.

Exit codes: 0 score >= --min-score, 1 score below --min-score, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATUS_FACTOR = {"pass": 1.0, "warn": 0.5, "fail": 0.0}

WEIGHTS = {
    "verify_command": 20,
    "verify_runs": 10,
    "boundaries": 15,
    "secrets": 15,
    "dead_code": 10,
    "agent_context": 10,
    "ci": 10,
    "duplication": 5,
    "tests": 5,
}

SKIP_DIRS = {".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv",
             "__pycache__", ".next", "coverage", "vendor", "target"}

SECRET_PATTERNS = [
    ("access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("git-host-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("chat-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("live-secret-key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
    ("api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("generic-assignment", re.compile(
        r"(?i)\b(?:password|passwd|secret|api_?key|access_?token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]")),
]

TEXT_SUFFIX_SKIP = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".jar",
                    ".dll", ".exe", ".woff", ".woff2", ".ttf", ".lock", ".svg", ".mp4", ".webp"}


class Env:
    """External calls, injectable for tests."""

    def __init__(self, which=shutil.which, run=subprocess.run, clock=time.monotonic):
        self.which = which
        self.run = run
        self.clock = clock


def check(cid: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "status": status, "weight": WEIGHTS[cid], "detail": detail,
            "fix": fix if status != "pass" else ""}


# ---------------------------------------------------------------- repo facts

def list_files(root: Path, env: Env) -> list[str]:
    """Tracked + untracked-not-ignored files (posix, relative). Falls back to a walk."""
    if (root / ".git").exists() and env.which("git"):
        try:
            proc = env.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                            "--exclude-standard"], capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                return sorted({line for line in proc.stdout.splitlines() if line.strip()})
        except (OSError, subprocess.SubprocessError):
            pass
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            out.append((Path(dirpath) / name).relative_to(root).as_posix())
    return sorted(out)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


NON_PROJECT_DIRS = {"fixtures", "testdata", "test-data", "examples", "samples", "node_modules", "vendor"}


def stack_marker(rel: str, max_depth: int) -> bool:
    """True if a project file sits shallow enough and outside fixture/example dirs."""
    parts = rel.split("/")
    return len(parts) - 1 <= max_depth and not NON_PROJECT_DIRS.intersection(parts[:-1])


def detect_stack(root: Path, files: list[str]) -> dict:
    pkg = read_json(root / "package.json") if (root / "package.json").exists() else None
    deps = {}
    if pkg:
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            if isinstance(pkg.get(key), dict):
                deps.update(pkg[key])
    sln = [f for f in files if f.endswith((".sln", ".slnx")) and stack_marker(f, max_depth=1)]
    csproj = [f for f in files if f.endswith(".csproj") and stack_marker(f, max_depth=3)]
    if pkg is not None:
        is_ts = "typescript" in deps or any(
            f.endswith("tsconfig.json") and stack_marker(f, max_depth=2) for f in files)
        name = "typescript" if is_ts else "javascript"
    elif sln or csproj:
        name = "dotnet"
    else:
        name = "unknown"
    return {"name": name, "package": pkg or {}, "deps": deps, "sln": sln, "csproj": csproj,
            "dotnet": bool(sln or csproj)}


def detect_verify(root: Path, files: list[str], stack: dict) -> str | None:
    """Return the single verify command for the repo, or None."""
    scripts = stack["package"].get("scripts") if isinstance(stack["package"].get("scripts"), dict) else {}
    if scripts:
        pm = "npm"
        if (root / "pnpm-lock.yaml").exists():
            pm = "pnpm"
        elif (root / "yarn.lock").exists():
            pm = "yarn"
        elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            pm = "bun"
        for name in ("verify", "check"):
            if name in scripts:
                return f"{pm} run {name}"
    makefile = root / "Makefile"
    if makefile.exists() and re.search(r"(?m)^verify\s*:", read_text(makefile)):
        return "make verify"
    for jf in ("justfile", "Justfile", ".justfile"):
        if (root / jf).exists() and re.search(r"(?m)^@?verify\b[^:\n]*:", read_text(root / jf)):
            return "just verify"
    for script in ("verify.sh", "scripts/verify.sh"):
        if (root / script).exists():
            return f"bash {script}"
    for script in ("verify.ps1", "scripts/verify.ps1"):
        if (root / script).exists():
            return f"pwsh {script}"
    if stack["sln"]:
        return f"dotnet test {stack['sln'][0]}"
    return None


# ---------------------------------------------------------------- checks

def check_verify_command(verify: str | None, stack: dict) -> dict:
    if verify:
        return check("verify_command", "pass", f"verify command: {verify}")
    return check("verify_command", "fail", "no single verify command found",
                 "Add one verify entry point that chains build, lint and test "
                 "(package.json \"verify\" script or scripts/verify.sh).")


def check_verify_runs(root: Path, verify: str | None, runs: int, slow_seconds: float,
                      timeout: float, env: Env) -> dict:
    if runs <= 0:
        return check("verify_runs", "skipped", "not run (use --runs N to time verify and detect flakiness)")
    if not verify:
        return check("verify_runs", "skipped", "no verify command to run")
    results, durations = [], []
    for _ in range(runs):
        start = env.clock()
        try:
            proc = env.run(shlex.split(verify), cwd=str(root), capture_output=True, text=True,
                           timeout=timeout)
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        except OSError as exc:
            return check("verify_runs", "skipped", f"skipped: could not run verify ({exc})")
        durations.append(env.clock() - start)
        results.append(ok)
    median = statistics.median(durations)
    passes = sum(results)
    detail = f"{passes}/{runs} passed, median {median:.1f}s"
    if 0 < passes < runs:
        return check("verify_runs", "fail", f"flaky: {detail}",
                     "Find and fix the flaky step; rerun with --runs to confirm it is stable.")
    if passes == 0:
        return check("verify_runs", "fail", f"verify fails: {detail}",
                     "Make verify pass on a clean checkout before adding agents.")
    if median > slow_seconds:
        return check("verify_runs", "warn", f"slow: {detail} (limit {slow_seconds:.0f}s)",
                     "Cut verify time: run cheap steps first, parallelize tests, move slow suites to CI.")
    return check("verify_runs", "pass", detail)


def check_boundaries(root: Path, files: list[str], stack: dict) -> dict:
    name = stack["name"]
    if name in ("typescript", "javascript"):
        configs = [f for f in files if re.search(r"(^|/)\.dependency-cruiser\.(c?js|mjs|json)$", f)
                   or re.search(r"(^|/)\.madgerc$", f)]
        tools = [d for d in ("dependency-cruiser", "madge", "eslint-plugin-boundaries") if d in stack["deps"]]
        if configs:
            return check("boundaries", "pass", f"boundary config: {', '.join(configs[:3])}")
        if tools:
            return check("boundaries", "warn", f"{', '.join(tools)} installed but no rules config found",
                         "Write module boundary and no-circular rules (dependency-cruiser config).")
        return check("boundaries", "fail", "no import-cycle or boundary rules",
                     "Add dependency-cruiser with no-circular and cross-module rules; "
                     "see the enforce-module-boundaries skill.")
    if name == "dotnet":
        hits = []
        for proj in stack["csproj"]:
            text = read_text(root / proj)
            if "ArchUnitNET" in text or "NetArchTest" in text:
                hits.append(proj)
        if hits:
            return check("boundaries", "pass", f"architecture tests referenced in {', '.join(hits[:3])}")
        return check("boundaries", "fail", "no ArchUnitNET or NetArchTest reference",
                     "Add an architecture test project that forbids cross-module dependencies; "
                     "see the enforce-module-boundaries skill.")
    return check("boundaries", "skipped", "skipped: unknown stack")


def check_dead_code(root: Path, files: list[str], stack: dict) -> dict:
    name = stack["name"]
    if name in ("typescript", "javascript"):
        configs = [f for f in files if re.search(r"(^|/)\.?knip\.(json|jsonc|ts|js)$", f)]
        if configs or "knip" in stack["package"]:
            return check("dead_code", "pass", "knip configured")
        tools = [d for d in ("knip", "ts-prune", "ts-unused-exports") if d in stack["deps"]]
        if tools:
            return check("dead_code", "pass", f"dead-code tool installed: {', '.join(tools)}")
        return check("dead_code", "fail", "no dead-code tool",
                     "Add knip and run it in verify or CI to catch unused files, exports and deps.")
    if name == "dotnet":
        props = [f for f in files if f.endswith(("Directory.Build.props", ".csproj"))]
        for f in props:
            text = read_text(root / f)
            if re.search(r"<(EnableNETAnalyzers|AnalysisLevel|AnalysisMode)>", text):
                return check("dead_code", "pass", f"analyzers configured in {f}")
        editorconfig = root / ".editorconfig"
        if editorconfig.exists() and re.search(r"(?i)dotnet_diagnostic\.(IDE0051|IDE0052|CA1823|IDE0060)",
                                               read_text(editorconfig)):
            return check("dead_code", "pass", "unused-code diagnostics set in .editorconfig")
        return check("dead_code", "warn", "no explicit analyzer settings",
                     "Set <AnalysisLevel>latest-recommended</AnalysisLevel> and raise IDE0051/IDE0052 "
                     "(unused members) to warning in .editorconfig.")
    return check("dead_code", "skipped", "skipped: unknown stack")


def check_duplication(root: Path, files: list[str], stack: dict, env: Env) -> dict:
    configs = [f for f in files if re.search(r"(^|/)\.jscpd(rc)?(\.json)?$", f)]
    if configs or "jscpd" in stack["deps"]:
        return check("duplication", "pass", "jscpd configured")
    if env.which("jscpd"):
        return check("duplication", "warn", "jscpd installed but not configured for this repo",
                     "Add a .jscpd.json with a threshold and run it in CI.")
    return check("duplication", "warn", "no duplication check (jscpd not found)",
                 "Add jscpd with a threshold, or track duplication with shrink-quality-budgets.")


def regex_secret_scan(root: Path, files: list[str], max_bytes: int = 512_000) -> list[dict]:
    hits = []
    for rel in files:
        path = root / rel
        if path.suffix.lower() in TEXT_SUFFIX_SKIP or rel.endswith(("package-lock.json", ".min.js")):
            continue
        try:
            if not path.is_file() or path.stat().st_size > max_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append({"file": rel, "line": lineno, "kind": kind})
                    break
    return hits


def check_secrets(root: Path, files: list[str], env: Env) -> dict:
    if env.which("gitleaks"):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            try:
                proc = env.run(["gitleaks", "detect", "--no-banner", "--redact", "--source", str(root),
                                "--report-format", "json", "--report-path", str(report)],
                               capture_output=True, text=True, timeout=300)
            except (OSError, subprocess.SubprocessError) as exc:
                proc = None
                err = str(exc)
            if proc is not None and proc.returncode in (0, 1):
                findings = []
                if report.exists():
                    try:
                        findings = json.loads(report.read_text() or "[]")
                    except ValueError:
                        findings = []
                if proc.returncode == 0 and not findings:
                    return check("secrets", "pass", "gitleaks: no leaks")
                n = len(findings) if findings else "some"
                return check("secrets", "fail", f"gitleaks: {n} finding(s)",
                             "Rotate the leaked secrets, remove them from history, add gitleaks to CI.")
            if proc is not None:
                err = (proc.stderr or "").strip()[:200]
            # fall through to regex scan
            detail_prefix = f"gitleaks error ({err}); "
    else:
        detail_prefix = "gitleaks not found; "
    hits = regex_secret_scan(root, files)
    if hits:
        where = ", ".join(f"{h['file']}:{h['line']} ({h['kind']})" for h in hits[:5])
        return check("secrets", "fail", f"{detail_prefix}regex scan: {len(hits)} possible secret(s): {where}",
                     "Remove or rotate the secrets and add a secret scanner (gitleaks) to CI.")
    return check("secrets", "warn" if "not found" in detail_prefix else "pass",
                 f"{detail_prefix}regex scan found nothing obvious",
                 "Add gitleaks to CI for a real secret scan.")


def check_agent_context(root: Path, files: list[str]) -> dict:
    found = [f for f in ("AGENTS.md", "CLAUDE.md", ".cursorrules", ".github/copilot-instructions.md")
             if (root / f).exists()]
    if (root / ".cursor" / "rules").exists():
        found.append(".cursor/rules")
    if found:
        return check("agent_context", "pass", f"agent context: {', '.join(found)}")
    return check("agent_context", "fail", "no agent context file",
                 "Add AGENTS.md with the verify command, layout, and house rules an agent must follow.")


def check_ci(root: Path, files: list[str]) -> dict:
    patterns = [r"^\.github/workflows/[^/]+\.ya?ml$", r"^\.gitlab-ci\.yml$", r"^azure-pipelines\.ya?ml$",
                r"^\.circleci/config\.ya?ml$", r"^Jenkinsfile$", r"^bitbucket-pipelines\.yml$",
                r"^\.buildkite/", r"^\.drone\.yml$", r"^\.travis\.yml$"]
    found = [f for f in files if any(re.search(p, f) for p in patterns)]
    if found:
        return check("ci", "pass", f"CI config: {', '.join(found[:3])}")
    return check("ci", "fail", "no CI config", "Add CI that runs the verify command on every push and PR.")


def check_tests(files: list[str]) -> dict:
    dir_re = re.compile(r"(^|/)(tests?|__tests__|specs?)/")
    file_re = re.compile(r"(\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test_[^/]+\.py$|_test\.(py|go)$|Tests?\.cs$)")
    proj_re = re.compile(r"(^|/)[^/]*\.Tests?\.csproj$")
    hits = [f for f in files if dir_re.search(f) or file_re.search(f) or proj_re.search(f)]
    if hits:
        return check("tests", "pass", f"{len(hits)} test file(s), e.g. {hits[0]}")
    return check("tests", "fail", "no tests found", "Add a test directory and at least one test run by verify.")


# ---------------------------------------------------------------- scoring

def score(checks: list[dict]) -> int:
    scored = [c for c in checks if c["status"] in STATUS_FACTOR]
    total = sum(c["weight"] for c in scored)
    if not total:
        return 0
    got = sum(c["weight"] * STATUS_FACTOR[c["status"]] for c in scored)
    return round(100 * got / total)


def ranked_fixes(checks: list[dict]) -> list[dict]:
    todo = [c for c in checks if c["status"] in ("fail", "warn")]
    todo.sort(key=lambda c: (-(c["weight"] * (1 - STATUS_FACTOR[c["status"]])), -c["weight"], c["id"]))
    return [{"rank": i, "id": c["id"], "status": c["status"],
             "impact": round(c["weight"] * (1 - STATUS_FACTOR[c["status"]]), 1), "fix": c["fix"]}
            for i, c in enumerate(todo, 1)]


def assess(root: Path, runs: int = 0, verify_cmd: str | None = None, slow_seconds: float = 600,
           timeout: float = 1800, env: Env | None = None) -> dict:
    env = env or Env()
    files = list_files(root, env)
    stack = detect_stack(root, files)
    verify = verify_cmd or detect_verify(root, files, stack)
    checks = [
        check_verify_command(verify, stack),
        check_verify_runs(root, verify, runs, slow_seconds, timeout, env),
        check_boundaries(root, files, stack),
        check_dead_code(root, files, stack),
        check_duplication(root, files, stack, env),
        check_secrets(root, files, env),
        check_agent_context(root, files),
        check_ci(root, files),
        check_tests(files),
    ]
    return {"root": str(root), "stack": stack["name"], "verify_command": verify,
            "score": score(checks), "checks": checks, "fixes": ranked_fixes(checks)}


def human(report: dict) -> str:
    lines = [f"agent readiness: {report['score']}/100  (stack: {report['stack']})", ""]
    for c in report["checks"]:
        lines.append(f"  {c['status']:<8} {c['id']:<15} w{c['weight']:<3} {c['detail']}")
    if report["fixes"]:
        lines += ["", "fix first:"]
        for f in report["fixes"]:
            lines.append(f"  {f['rank']}. [{f['id']}, +{f['impact']:g}] {f['fix']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a repo for agent readiness and rank fixes.")
    parser.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    parser.add_argument("--runs", type=int, default=0,
                        help="run verify N times to time it and detect flakiness (default 0 = skip)")
    parser.add_argument("--verify-cmd", help="override the detected verify command")
    parser.add_argument("--slow-seconds", type=float, default=600, help="median verify time to warn above")
    parser.add_argument("--timeout", type=float, default=1800, help="timeout per verify run, seconds")
    parser.add_argument("--min-score", type=int, default=0, help="exit 1 if score is below this (default 0)")
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    if args.runs < 0:
        print("error: --runs must be >= 0", file=sys.stderr)
        return 2
    report = assess(root, args.runs, args.verify_cmd, args.slow_seconds, args.timeout, env)
    report["min_score"] = args.min_score
    print(json.dumps(report, indent=2) if args.json else human(report))
    return 0 if report["score"] >= args.min_score else 1


if __name__ == "__main__":
    sys.exit(main())
