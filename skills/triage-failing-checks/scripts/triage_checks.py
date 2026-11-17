#!/usr/bin/env python3
"""Triage failing checks: regression, already-broken, or flaky.

Runs a check command on HEAD (the current worktree, optionally N times) and once on the base
commit in a temporary detached worktree, then labels each failure. Tracks rework rounds in a
state file and refuses to run past --max-rounds so the agent stops and escalates.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

COMMAND_ID = "(command)"
DEFAULT_BASES = ("origin/main", "origin/master", "main", "master")
OUTPUT_TAIL = 2000

REGRESSION = "regression"
ALREADY_BROKEN = "already-broken"
FLAKY = "flaky"
UNCLEAR = "unclear"
FIXED = "fixed"
BLOCKING = (REGRESSION, FLAKY)


class UsageError(Exception):
    pass


class Runner:
    """Shell and git access. Swap in a fake for tests."""

    def shell(self, cmd: str, cwd: str, timeout: float | None) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout, text=True, errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return 124, out + f"\n[timeout after {timeout}s]"
        return proc.returncode, proc.stdout

    def git(self, args: list[str], cwd: str) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return proc.returncode, (proc.stdout if proc.returncode == 0 else proc.stderr).strip()


# ---------- parsing ----------

def parse_spec(spec: str | None) -> tuple[str, str] | None:
    if not spec:
        return None
    kind, sep, value = spec.partition(":")
    if not sep or kind not in ("junit", "regex") or not value:
        raise UsageError("--parse must be junit:<path> or regex:<pattern>")
    if kind == "regex":
        try:
            re.compile(value)
        except re.error as exc:
            raise UsageError(f"bad --parse regex: {exc}") from exc
    return kind, value


def junit_failures(path: str, since: float | None = None) -> list[str] | None:
    """Failed/errored testcase ids from a JUnit XML file or a directory of them.

    Files older than `since` are ignored (stale reports from an earlier run). Returns None
    if no fresh report was found.
    """
    files: list[str] = []
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            files.extend(os.path.join(root, n) for n in names if n.endswith(".xml"))
    elif os.path.isfile(path):
        files.append(path)
    if since is not None:
        files = [f for f in files if os.path.getmtime(f) >= since - 1]
    if not files:
        return None
    ids: list[str] = []
    for f in sorted(files):
        try:
            tree = ET.parse(f)
        except ET.ParseError:
            continue
        for case in tree.iter("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            name = case.get("name", "?")
            cls = case.get("classname")
            ids.append(f"{cls}.{name}" if cls else name)
    return sorted(set(ids))


def regex_failures(pattern: str, output: str) -> list[str]:
    rx = re.compile(pattern, re.MULTILINE)
    ids = []
    for m in rx.finditer(output):
        if "id" in rx.groupindex:
            ids.append(m.group("id"))
        elif rx.groups:
            ids.append(m.group(1))
        else:
            ids.append(m.group(0))
    return sorted({i.strip() for i in ids if i and i.strip()})


# ---------- running ----------

def run_once(runner: Runner, cmd: str, cwd: str, parse: tuple[str, str] | None,
             ids: list[str], timeout: float | None) -> dict:
    """Run the check once in cwd. Returns {exit_code, failed, error, output_tail, duration_s}."""
    start = time.time()
    if ids and "{id}" in cmd:
        failed, codes, tails = [], [], []
        for test_id in ids:
            code, out = runner.shell(cmd.replace("{id}", shlex.quote(test_id)), cwd, timeout)
            codes.append(code)
            tails.append(out[-OUTPUT_TAIL // max(len(ids), 1):])
            if code != 0:
                failed.append(test_id)
        return {
            "exit_code": max(codes) if codes else 0,
            "failed": sorted(failed),
            "error": False,
            "output_tail": "\n".join(tails)[-OUTPUT_TAIL:],
            "duration_s": round(time.time() - start, 2),
        }

    code, out = runner.shell(cmd, cwd, timeout)
    failed: list[str] | None
    if parse is None:
        failed = [COMMAND_ID] if code != 0 else []
    elif parse[0] == "junit":
        failed = junit_failures(os.path.join(cwd, parse[1]), since=start)
    else:
        failed = regex_failures(parse[1], out)
    error = False
    if failed is None:
        failed = []
        if code != 0:
            error = True
    if parse is not None and code != 0 and not failed:
        # Command failed but nothing parsed: build error, crash, or wrong parser.
        error = True
        failed = [COMMAND_ID]
    if ids:
        wanted = set(ids)
        failed = [f for f in failed if f in wanted or f == COMMAND_ID]
    return {
        "exit_code": code,
        "failed": sorted(failed),
        "error": error,
        "output_tail": out[-OUTPUT_TAIL:],
        "duration_s": round(time.time() - start, 2),
    }


def classify(head_runs: list[dict], base_run: dict | None) -> list[dict]:
    """Label every id that failed anywhere."""
    all_ids: set[str] = set()
    for r in head_runs:
        all_ids.update(r["failed"])
    if base_run:
        all_ids.update(base_run["failed"])
    base_error = bool(base_run and base_run["error"])
    result = []
    for test_id in sorted(all_ids):
        head = ["fail" if test_id in r["failed"] else "pass" for r in head_runs]
        if base_run is None:
            base = "skipped"
        elif base_error and test_id not in base_run["failed"]:
            base = "unknown"
        else:
            base = "fail" if test_id in base_run["failed"] else "pass"
        n_fail = head.count("fail")
        if 0 < n_fail < len(head):
            label = FLAKY
        elif n_fail == 0:
            label = FIXED if base == "fail" else None
        elif base == "fail":
            label = ALREADY_BROKEN
        elif base == "pass":
            label = REGRESSION
        else:
            label = UNCLEAR
        if label:
            result.append({"id": test_id, "label": label, "head": head, "base": base})
    return result


# ---------- git ----------

def resolve_base(runner: Runner, repo: str, base: str | None) -> tuple[str, str]:
    """Return (ref description, commit sha) of the merge-base to compare against."""
    candidates = [base] if base else list(DEFAULT_BASES)
    for ref in candidates:
        code, sha = runner.git(["merge-base", "HEAD", ref], repo)
        if code == 0 and sha:
            return f"merge-base(HEAD, {ref})", sha
    if base:
        raise UsageError(f"cannot find merge-base of HEAD and {base}")
    raise UsageError("no base found (tried origin/main, origin/master, main, master); pass --base")


class BaseWorktree:
    """Temporary detached worktree at a commit. Always removed on exit."""

    def __init__(self, runner: Runner, repo: str, sha: str):
        self.runner, self.repo, self.sha = runner, repo, sha
        self.tmp = ""
        self.path = ""

    def __enter__(self) -> str:
        self.tmp = tempfile.mkdtemp(prefix="triage-base-")
        self.path = os.path.join(self.tmp, "wt")
        code, err = self.runner.git(["worktree", "add", "--detach", self.path, self.sha], self.repo)
        if code != 0:
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise RuntimeError(f"git worktree add failed: {err}")
        return self.path

    def __exit__(self, *exc) -> None:
        self.runner.git(["worktree", "remove", "--force", self.path], self.repo)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.runner.git(["worktree", "prune"], self.repo)


# ---------- rounds ----------

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("rounds"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"rounds": {}}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def round_key(runner: Runner, repo: str, cmd: str) -> str:
    code, branch = runner.git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    if code != 0 or branch == "HEAD":
        branch = "detached"
    return f"{branch}::{cmd}"


# ---------- main ----------

def triage(args: argparse.Namespace, runner: Runner) -> tuple[int, dict]:
    if not args.cmd:
        raise UsageError("--cmd is required")
    parse = parse_spec(args.parse)
    ids = list(args.ids or [])
    if args.ids_file:
        try:
            with open(args.ids_file, encoding="utf-8") as fh:
                ids.extend(line.strip() for line in fh if line.strip() and not line.startswith("#"))
        except OSError as exc:
            raise UsageError(f"cannot read --ids-file: {exc}") from exc
    if ids and "{id}" not in args.cmd and parse is None:
        raise UsageError("with test ids, put {id} in --cmd or give --parse to read ids from output")
    if args.repeat < 1:
        raise UsageError("--repeat must be >= 1")
    if args.max_rounds < 1:
        raise UsageError("--max-rounds must be >= 1")

    start_dir = os.path.realpath(args.repo)
    code, top = runner.git(["rev-parse", "--show-toplevel"], start_dir)
    if code != 0:
        raise UsageError(f"not a git repo: {start_dir}")
    top = os.path.realpath(top)
    rel = os.path.relpath(start_dir, top)
    if rel == ".." or rel.startswith(".." + os.sep):
        rel = "."

    state_path = None if args.no_state else (args.state or os.path.join(top, ".triage-rounds.json"))
    key = round_key(runner, top, args.cmd)
    state = load_state(state_path) if state_path else {"rounds": {}}
    if args.reset_rounds and state_path:
        state["rounds"].pop(key, None)
        save_state(state_path, state)
    entry = state["rounds"].get(key, {"count": 0, "history": []})
    this_round = entry["count"] + 1

    report: dict = {
        "status": None, "cmd": args.cmd, "repeat": args.repeat, "round": this_round,
        "max_rounds": args.max_rounds, "state": state_path, "failures": [], "runs": [],
        "skipped": [], "message": "",
    }

    if state_path and this_round > args.max_rounds:
        report["round"] = entry["count"]
        report["status"] = "escalate"
        report["message"] = (
            f"{entry['count']} rework rounds already spent on this branch and check (max {args.max_rounds}). "
            "Stop. Escalate to a human with the last report; do not attempt another fix. "
            "A human can clear the count with --reset-rounds."
        )
        report["history"] = entry.get("history", [])
        return 1, report

    _, head_sha = runner.git(["rev-parse", "HEAD"], top)
    report["head"] = head_sha
    _, dirty = runner.git(["status", "--porcelain"], top)
    report["head_dirty"] = bool(dirty)

    head_runs = []
    for attempt in range(1, args.repeat + 1):
        r = run_once(runner, args.cmd, start_dir, parse, ids, args.timeout)
        r.update({"where": "head", "attempt": attempt})
        head_runs.append(r)
    report["runs"].extend(head_runs)

    base_run = None
    any_head_fail = any(r["failed"] for r in head_runs)
    if args.skip_base:
        report["skipped"].append("base run: --skip-base")
    elif not any_head_fail:
        report["skipped"].append("base run: no failures on head")
    else:
        base_desc, base_sha = resolve_base(runner, top, args.base)
        report["base"] = base_sha
        report["base_ref"] = base_desc
        with BaseWorktree(runner, top, base_sha) as wt:
            cwd = os.path.join(wt, rel) if rel != "." else wt
            if not os.path.isdir(cwd):
                cwd = wt
            if args.setup:
                scode, sout = runner.shell(args.setup, cwd, args.timeout)
                report["base_setup"] = {"exit_code": scode, "output_tail": sout[-OUTPUT_TAIL:]}
            base_run = run_once(runner, args.cmd, cwd, parse, ids, args.timeout)
            base_run.update({"where": "base", "attempt": 1})
        report["runs"].append(base_run)

    failures = classify(head_runs, base_run)
    report["failures"] = failures
    counts: dict[str, int] = {}
    for f in failures:
        counts[f["label"]] = counts.get(f["label"], 0) + 1
    report["counts"] = counts

    blocking = [f for f in failures if f["label"] in BLOCKING]
    if blocking:
        report["status"] = "blocking"
    elif any(f["label"] in (ALREADY_BROKEN, UNCLEAR) for f in failures):
        report["status"] = "non-blocking"
    else:
        report["status"] = "clean"

    if state_path:
        if any_head_fail:
            entry["count"] = this_round
            entry.setdefault("history", []).append({
                "at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "head": head_sha,
                "counts": counts,
            })
            entry["history"] = entry["history"][-10:]
            state["rounds"][key] = entry
        else:
            state["rounds"].pop(key, None)
        save_state(state_path, state)
    if not any_head_fail:
        report["round"] = 0

    left = args.max_rounds - report["round"]
    if report["status"] == "blocking":
        report["message"] = (
            f"Round {report['round']} of {args.max_rounds}. Fix regressions; investigate flaky failures. "
            + ("This was the last round: if the next fix does not land, escalate." if left <= 0 else f"{left} round(s) left.")
        )
    elif report["status"] == "non-blocking":
        report["message"] = "Only failures that also fail on base (or base was unclear). Not caused by this change; report them, do not fix in this change."
    else:
        report["message"] = "No failures on head."
    return (1 if blocking else 0), report


def summarize(report: dict) -> str:
    lines = [f"status: {report['status']}  round: {report['round']}/{report['max_rounds']}"]
    if report.get("head"):
        dirty = " (uncommitted changes)" if report.get("head_dirty") else ""
        lines.append(f"head: {report['head'][:12]}{dirty}")
    if report.get("base"):
        lines.append(f"base: {report['base'][:12]} {report.get('base_ref', '')}")
    if report.get("base_setup") and report["base_setup"]["exit_code"] != 0:
        lines.append(f"warning: base --setup exited {report['base_setup']['exit_code']}")
    for r in report.get("runs", []):
        flag = " (no parseable results)" if r.get("error") else ""
        lines.append(f"  {r['where']} #{r['attempt']}: exit {r['exit_code']}, {len(r['failed'])} failed, {r['duration_s']}s{flag}")
    for f in report.get("failures", []):
        lines.append(f"  [{f['label']}] {f['id']}  head={','.join(f['head'])} base={f['base']}")
    for s in report.get("skipped", []):
        lines.append(f"skipped: {s}")
    if report.get("message"):
        lines.append(report["message"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Label failing checks as regression, already-broken, or flaky by running them on HEAD and on the merge-base.",
    )
    p.add_argument("--cmd", help="check command, run through the shell (e.g. \"npm test\"); use {id} to run once per test id")
    p.add_argument("--ids", nargs="+", metavar="ID", help="failing test ids to triage")
    p.add_argument("--ids-file", help="file with one failing test id per line")
    p.add_argument("--parse", help="read failure ids from output: junit:<path to xml file or dir> or regex:<pattern with (?P<id>...) or one group>")
    p.add_argument("--base", help="ref to compare against (default: merge-base of HEAD with origin/main, origin/master, main, or master)")
    p.add_argument("--repeat", type=int, default=1, help="runs on head, to detect flakiness (default %(default)s)")
    p.add_argument("--setup", help="command to run in the base worktree before the check (e.g. \"npm ci\")")
    p.add_argument("--skip-base", action="store_true", help="do not run on base (flakiness check only)")
    p.add_argument("--timeout", type=float, default=1800, help="seconds per command run (default %(default)s)")
    p.add_argument("--repo", default=".", help="directory to run in, inside a git repo (default: current dir)")
    p.add_argument("--state", help="rounds state file (default: <repo root>/.triage-rounds.json)")
    p.add_argument("--max-rounds", type=int, default=3, help="rework rounds before refusing and escalating (default %(default)s)")
    p.add_argument("--reset-rounds", action="store_true", help="clear the round count for this branch and command first")
    p.add_argument("--no-state", action="store_true", help="do not read or write the rounds state file")
    p.add_argument("--json", action="store_true", help="print machine JSON")
    return p


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = runner or Runner()
    if runner.__class__ is Runner and shutil.which("git") is None:
        print("error: git not found", file=sys.stderr)
        return 2
    try:
        code, report = triage(args, runner)
    except UsageError as exc:
        if args.json:
            print(json.dumps({"status": "error", "message": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(summarize(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
