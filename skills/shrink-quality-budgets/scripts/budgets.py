#!/usr/bin/env python3
"""Quality budgets that only go down.

quality-budgets.json holds an upper bound per metric. `measure` prints current
values, `check` fails when any metric is above its budget, `check --tighten`
lowers budgets to current values (never raises), `init` writes a budgets file
at current values.

Metrics: loc, comment_lines (stdlib counter for ts/tsx/js/jsx/mjs/cjs/cs/py/go/java),
duplication (jscpd %), dead_code (knip issue count), boundary_violations
(dependency-cruiser report or run, else the enforce-module-boundaries baseline).
Metrics whose tool or report is missing are skipped, never failed.

Exit codes: 0 ok, 1 over budget, 2 usage/input error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

METRICS = ("loc", "comment_lines", "duplication", "dead_code", "boundary_violations")
C_LIKE = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".cs", ".go", ".java"}
HASH = {".py"}
SKIP_DIRS = {".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv", "__pycache__",
             "vendor", "target", "coverage", ".next"}
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
DEFAULT_CONFIG = "quality-budgets.json"
DEFAULT_BASELINE = "boundaries-baseline.json"
DEPCRUISE_CONFIGS = (".dependency-cruiser.cjs", ".dependency-cruiser.js", ".dependency-cruiser.mjs",
                     ".dependency-cruiser.json")
RAISE_NOTE = "Raising a budget is a deliberate manual edit to the budgets file; --tighten never raises."


class Env:
    """External calls, injectable for tests."""

    def __init__(self, which=shutil.which, run=subprocess.run):
        self.which = which
        self.run = run


# ---------------------------------------------------------------- globs and files

def glob_to_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1:j].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, patterns: list[str]) -> bool:
    """gitignore-like: a pattern without '/' also matches the basename at any depth."""
    base = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.startswith("./"):
            pattern = pattern[2:]
        if pattern.endswith("/"):
            pattern += "**"
        rx = glob_to_regex(pattern)
        if rx.match(path) or ("/" not in pattern and rx.match(base)):
            return True
    return False


def list_files(root: Path, env: Env) -> list[str]:
    if env.which("git"):
        try:
            probe = env.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, timeout=30)
            if probe.returncode == 0 and probe.stdout.strip() == "true":
                proc = env.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
                               capture_output=True, text=True, timeout=120)
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


def source_files(root: Path, include: list[str], exclude: list[str], env: Env) -> list[str]:
    files = []
    for rel in list_files(root, env):
        if Path(rel).suffix.lower() not in C_LIKE | HASH:
            continue
        if include and not matches(rel, include):
            continue
        if exclude and matches(rel, exclude):
            continue
        files.append(rel)
    return files


# ---------------------------------------------------------------- line counting

def classify_c(line: str, in_block: bool) -> tuple[str, bool]:
    s = STRING_RE.sub('""', line.strip())
    code = comment = False
    i, n = 0, len(s)
    while i < n:
        if in_block:
            comment = True
            j = s.find("*/", i)
            if j < 0:
                break
            in_block, i = False, j + 2
            continue
        if s.startswith("//", i):
            comment = True
            break
        if s.startswith("/*", i):
            in_block, comment, i = True, True, i + 2
            continue
        if not s[i].isspace():
            code = True
        i += 1
    return ("code" if code else "comment" if comment else "blank"), in_block


def count_text(text: str, suffix: str) -> dict:
    loc = comments = 0
    in_block = False
    for line in text.splitlines():
        if suffix in HASH:
            stripped = line.strip()
            kind = "blank" if not stripped else "comment" if stripped.startswith("#") else "code"
        else:
            kind, in_block = classify_c(line, in_block)
        if kind == "code":
            loc += 1
        elif kind == "comment":
            comments += 1
    return {"loc": loc, "comment_lines": comments}


def count_files(root: Path, files: list[str]) -> dict:
    totals = {"loc": 0, "comment_lines": 0, "files": 0, "by_language": {}}
    for rel in files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        suffix = Path(rel).suffix.lower()
        counts = count_text(text, suffix)
        lang = totals["by_language"].setdefault(suffix.lstrip("."), {"files": 0, "loc": 0, "comment_lines": 0})
        lang["files"] += 1
        totals["files"] += 1
        for k in ("loc", "comment_lines"):
            lang[k] += counts[k]
            totals[k] += counts[k]
    return totals


# ---------------------------------------------------------------- tool metrics

def skipped(reason: str) -> dict:
    return {"value": None, "status": "skipped", "reason": reason}


def measured(value, source: str) -> dict:
    return {"value": value, "status": "measured", "source": source}


def read_json_file(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def local_bin(root: Path, name: str, env: Env) -> str | None:
    local = root / "node_modules" / ".bin" / name
    if local.exists():
        return str(local)
    return env.which(name)


def jscpd_percentage(data: dict) -> float:
    return round(float(data["statistics"]["total"]["percentage"]), 2)


def measure_duplication(root: Path, sources: dict, exclude: list[str], env: Env) -> dict:
    report = sources.get("jscpd_report")
    if report:
        path = root / report
        if not path.exists():
            return skipped(f"skipped: jscpd report not found: {report}")
        try:
            return measured(jscpd_percentage(read_json_file(path)), report)
        except (ValueError, KeyError, TypeError) as exc:
            return skipped(f"skipped: cannot read jscpd report ({exc})")
    exe = local_bin(root, "jscpd", env)
    if not exe:
        return skipped("skipped: jscpd not found")
    with tempfile.TemporaryDirectory() as tmp:
        argv = [exe, "--silent", "--gitignore", "--reporters", "json", "--output", tmp]
        if exclude:
            argv += ["--ignore", ",".join(exclude)]
        argv.append(str(root))
        try:
            env.run(argv, cwd=str(root), capture_output=True, text=True, timeout=900)
            return measured(jscpd_percentage(read_json_file(Path(tmp) / "jscpd-report.json")), "jscpd")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            return skipped(f"skipped: jscpd run failed ({exc})")


def knip_count(data: dict) -> int:
    total = len(data.get("files") or [])
    for issue in data.get("issues") or []:
        for k, v in issue.items():
            if k in ("file", "owners"):
                continue
            if isinstance(v, list):
                total += len(v)
            elif isinstance(v, dict):
                total += sum(len(x) if isinstance(x, (list, dict)) else 1 for x in v.values())
    return total


def measure_dead_code(root: Path, sources: dict, env: Env) -> dict:
    report = sources.get("knip_report")
    if report:
        path = root / report
        if not path.exists():
            return skipped(f"skipped: knip report not found: {report}")
        try:
            return measured(knip_count(read_json_file(path)), report)
        except (ValueError, AttributeError, TypeError) as exc:
            return skipped(f"skipped: cannot read knip report ({exc})")
    exe = local_bin(root, "knip", env)
    if not exe:
        return skipped("skipped: knip not found")
    try:
        proc = env.run([exe, "--reporter", "json", "--no-progress"], cwd=str(root),
                       capture_output=True, text=True, timeout=900)
        return measured(knip_count(json.loads(proc.stdout)), "knip")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError) as exc:
        return skipped(f"skipped: knip run failed ({exc})")


def depcruise_count(data: dict) -> int:
    violations = data["summary"]["violations"]
    return sum(1 for v in violations if (v.get("rule") or {}).get("severity") not in ("info", "ignore"))


def measure_boundaries(root: Path, sources: dict, env: Env) -> dict:
    report = sources.get("depcruise_report")
    if report:
        path = root / report
        if not path.exists():
            return skipped(f"skipped: dependency-cruiser report not found: {report}")
        try:
            return measured(depcruise_count(read_json_file(path)), report)
        except (ValueError, KeyError, TypeError) as exc:
            return skipped(f"skipped: cannot read dependency-cruiser report ({exc})")
    config = next((c for c in DEPCRUISE_CONFIGS if (root / c).exists()), None)
    exe = local_bin(root, "depcruise", env) if config else None
    if exe:
        targets = sources.get("depcruise_sources") or (["src"] if (root / "src").is_dir() else ["."])
        try:
            proc = env.run([exe, "--config", config, "--output-type", "json", *targets], cwd=str(root),
                           capture_output=True, text=True, timeout=900)
            return measured(depcruise_count(json.loads(proc.stdout)), "dependency-cruiser")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass  # fall back to the baseline
    baseline = root / sources.get("boundaries_baseline", DEFAULT_BASELINE)
    if baseline.exists():
        try:
            data = read_json_file(baseline)
            count = data.get("count")
            if not isinstance(count, int):
                count = len(data.get("violations") or [])
            return measured(count, baseline.relative_to(root).as_posix())
        except (ValueError, AttributeError, TypeError) as exc:
            return skipped(f"skipped: cannot read {baseline.name} ({exc})")
    return skipped("skipped: no dependency-cruiser report or run, no boundaries baseline")


# ---------------------------------------------------------------- commands

def load_config(path: Path, required: bool) -> dict:
    if not path.exists():
        if required:
            raise ValueError(f"budgets file not found: {path} (create it with `init`)")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"budgets file is not valid JSON: {path}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("budgets", {}), dict):
        raise ValueError(f"budgets file must be an object with a 'budgets' object: {path}")
    return data


def measure(root: Path, config: dict, env: Env) -> dict:
    include = config.get("include") or []
    exclude = config.get("exclude") or []
    sources = config.get("sources") or {}
    files = source_files(root, include, exclude, env)
    counts = count_files(root, files)
    return {
        "files": counts["files"],
        "by_language": counts["by_language"],
        "metrics": {
            "loc": measured(counts["loc"], "line counter"),
            "comment_lines": measured(counts["comment_lines"], "line counter"),
            "duplication": measure_duplication(root, sources, exclude, env),
            "dead_code": measure_dead_code(root, sources, env),
            "boundary_violations": measure_boundaries(root, sources, env),
        },
    }


def check(config: dict, metrics: dict, tighten: bool) -> dict:
    budgets = config.get("budgets", {})
    rows, over, changes = [], [], []
    for name in METRICS:
        m = metrics[name]
        budget = budgets.get(name)
        row = {"metric": name, "value": m["value"], "budget": budget}
        if m["status"] == "skipped":
            row.update(status="skipped", reason=m["reason"])
        elif budget is None:
            row.update(status="no-budget")
        elif m["value"] > budget:
            row.update(status="over", delta=round(m["value"] - budget, 2))
            over.append(name)
        else:
            row.update(status="ok", delta=round(m["value"] - budget, 2))
            if tighten and m["value"] < budget:
                changes.append({"metric": name, "from": budget, "to": m["value"]})
        rows.append(row)
    unknown = sorted(set(budgets) - set(METRICS))
    return {"status": "fail" if over else "pass", "over": over, "rows": rows, "tightened": changes,
            "unknown_budgets": unknown}


def write_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def fmt(v) -> str:
    return "-" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def human_measure(result: dict) -> str:
    lines = [f"{result['files']} source file(s)"]
    for name in METRICS:
        m = result["metrics"][name]
        lines.append(f"  {name:<20} {fmt(m['value']):>10}  " + (m.get("source", "") if m["status"] == "measured"
                                                                  else m["reason"]))
    return "\n".join(lines)


def human_check(result: dict, path: str) -> str:
    head = "PASS" if result["status"] == "pass" else f"FAIL ({len(result['over'])} over budget)"
    lines = [f"quality budgets ({path}): {head}", f"  {'metric':<20} {'value':>10} {'budget':>10}  status"]
    for r in result["rows"]:
        if r["status"] == "skipped":
            status = r["reason"]
        elif r["status"] == "no-budget":
            status = "no budget set"
        else:
            sign = "+" if r["delta"] > 0 else ""
            status = f"{r['status']} ({sign}{fmt(r['delta'])})"
        lines.append(f"  {r['metric']:<20} {fmt(r['value']):>10} {fmt(r['budget']):>10}  {status}")
    for c in result["tightened"]:
        lines.append(f"tightened: {c['metric']} {fmt(c['from'])} -> {fmt(c['to'])}")
    if result["unknown_budgets"]:
        lines.append("ignored unknown budgets: " + ", ".join(result["unknown_budgets"]))
    if result["status"] == "fail":
        lines.append("Bring the metric back under budget. " + RAISE_NOTE)
    return "\n".join(lines)


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".", help="repo root (default: .)")
    common.add_argument("--config", default=DEFAULT_CONFIG, help=f"budgets file, relative to --root (default: {DEFAULT_CONFIG})")
    common.add_argument("--json", action="store_true", help="print machine JSON")
    parser = argparse.ArgumentParser(description="Quality budgets that only go down.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", parents=[common], help="print current metric values")
    p_check = sub.add_parser("check", parents=[common], help="fail if any metric is above its budget")
    p_check.add_argument("--tighten", action="store_true", help="lower budgets to current values (never raises)")
    p_init = sub.add_parser("init", parents=[common], help="write a budgets file at current values")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing budgets file")
    args = parser.parse_args(argv)

    env = env or Env()
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    config_path = root / args.config
    try:
        config = load_config(config_path, required=args.command == "check")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "init" and config_path.exists() and not args.force:
        msg = f"{args.config} exists; edit it, run `check --tighten`, or pass --force"
        print(json.dumps({"action": "exists", "reason": msg}) if args.json else msg)
        return 0

    result = measure(root, config, env)
    if args.command == "measure":
        print(json.dumps(result, indent=2) if args.json else human_measure(result))
        return 0

    if args.command == "init":
        config.setdefault("include", [])
        config.setdefault("exclude", [])
        config["budgets"] = {k: m["value"] for k, m in result["metrics"].items() if m["status"] == "measured"}
        config.setdefault("sources", {})
        write_config(config_path, config)
        out = {"action": "wrote", "config": args.config, "budgets": config["budgets"],
               "skipped": {k: m["reason"] for k, m in result["metrics"].items() if m["status"] == "skipped"}}
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"wrote {args.config}: " + ", ".join(f"{k}={fmt(v)}" for k, v in out["budgets"].items()))
            for k, reason in out["skipped"].items():
                print(f"  {k}: {reason}")
        return 0

    outcome = check(config, result["metrics"], args.tighten)
    if outcome["tightened"]:
        for c in outcome["tightened"]:
            config["budgets"][c["metric"]] = c["to"]
        write_config(config_path, config)
    outcome["config"] = args.config
    outcome["note"] = RAISE_NOTE
    print(json.dumps(outcome, indent=2) if args.json else human_check(outcome, args.config))
    return 1 if outcome["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
