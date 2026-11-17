#!/usr/bin/env python3
"""Run the golden evals: each skill script against saved fixtures, compared to saved output.

Layout:
  evals/golden/<skill>/<case>/case.json
  evals/golden/<skill>/<case>/fixture/        (optional) copied to a temp dir per run
  evals/golden/<skill>/<case>/expected.json   the script's JSON stdout, normalized
  evals/golden/<skill>/<case>/expected.txt    used instead when stdout is not JSON

case.json:
  script       file name under skills/<skill>/scripts/ (required)
  args         list of args; "{fixture}" is the temp fixture dir, "{repo}" the repo root
  cwd          "fixture" (default) or "repo"
  expect_exit  expected exit code (required); --update never changes it
  setup        optional steps run in the temp fixture dir before the script:
                 ["git", "init", ...]                  a command (argv list; "python3" means this python)
                 {"copy": "patches/a.py", "to": "a.py"} copy a file from the case dir into the fixture
  tools        optional list of extra commands to put on PATH (default: only python3 and git)
  description  optional, one line

Setup and script runs get a PATH holding only python3, git, and the case's
`tools`, so optional tools (gh, npx, dotnet, jscpd, gitleaks, ...) are always
"not found"; plus a fixed git identity and commit dates, an empty global git
config, TZ=UTC, and PYTHONDONTWRITEBYTECODE=1. Results do not depend on the
machine.

Normalization (applied to actual output before comparing, and before --update writes):
  - the temp fixture dir -> "<TMP>"; any other path under the system temp dir -> "<TMP>"
  - the repo root -> "<REPO>"
  - 40/64-hex git SHAs anywhere -> "<SHA>"; shorter hex (7+) under keys like head/base/sha/commit -> "<SHA>"
  - numbers under timing keys (duration*, elapsed*, timing*, *_s, *_ms, *_sec, *_seconds),
    including nested ones -> 0
  - ISO timestamps under keys like at/timestamp/generated_at/*_at -> "<TIMESTAMP>";
    in expected.txt, any ISO date-time -> "<TIMESTAMP>"

Usage:
  python3 evals/run_golden.py [--skill NAME ...] [--changed-from REF] [--update] [--json]

Exit 0 when every case matches, 1 on any mismatch or broken case, 2 on usage error.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN_REL = Path("evals") / "golden"
RUN_ALL_PATHS = ("evals/run_golden.py", ".github/")
CASE_TIMEOUT = 300

GIT_ENV = {
    "GIT_AUTHOR_NAME": "golden",
    "GIT_AUTHOR_EMAIL": "golden@example.invalid",
    "GIT_COMMITTER_NAME": "golden",
    "GIT_COMMITTER_EMAIL": "golden@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}

SHA_FULL_RE = re.compile(r"\b(?:[0-9a-f]{64}|[0-9a-f]{40})\b")
SHA_SHORT_RE = re.compile(r"^[0-9a-f]{7,64}$")
SHA_KEYS = {"sha", "head", "base", "commit", "head_sha", "base_sha", "merge_base", "rev", "revision"}
TIMING_KEY_RE = re.compile(r"(duration|elapsed|timing)|(_s|_ms|_sec|_secs|_seconds)$", re.IGNORECASE)
TIMESTAMP_KEY_RE = re.compile(r"^(at|timestamp|time|now|generated|generated_at|date)$|_at$", re.IGNORECASE)
ISO_RE = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
ISO_FULL_RE = re.compile(rf"^{ISO_RE}$")
ISO_ANY_RE = re.compile(ISO_RE)


class CaseError(Exception):
    pass


# ---------------------------------------------------------------- normalization

class Normalizer:
    def __init__(self, tmp: str | None, repo: str | Path) -> None:
        pairs: list[tuple[str, str]] = []
        if tmp:
            for form in _path_forms(tmp):
                pairs.append((form, "<TMP>"))
        for form in _path_forms(str(repo)):
            pairs.append((form, "<REPO>"))
        # longest first so /private/var/x wins over /var/x
        self.pairs = sorted(set(pairs), key=lambda p: -len(p[0]))
        sys_tmp = sorted(_path_forms(tempfile.gettempdir()), key=len, reverse=True)
        self.tmp_re = re.compile(
            "(?:" + "|".join(re.escape(t.rstrip("/\\")) for t in sys_tmp) + r")[/\\][^/\\\s\"':,]+"
        )

    def text(self, s: str, iso: bool = False) -> str:
        for old, new in self.pairs:
            s = s.replace(old, new)
        s = self.tmp_re.sub("<TMP>", s)
        s = SHA_FULL_RE.sub("<SHA>", s)
        if iso:
            s = ISO_ANY_RE.sub("<TIMESTAMP>", s)
        return s

    def value(self, v, key: str | None = None, timing: bool = False):
        """Normalize parsed JSON. Numbers anywhere under a timing key become 0."""
        timing = timing or (key is not None and bool(TIMING_KEY_RE.search(key)))
        if isinstance(v, dict):
            return {k: self.value(x, k, timing) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x, key, timing) for x in v]
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            return 0 if timing else v
        if isinstance(v, str):
            if key is not None and key.lower() in SHA_KEYS and SHA_SHORT_RE.match(v):
                return "<SHA>"
            if key is not None and TIMESTAMP_KEY_RE.search(key) and ISO_FULL_RE.match(v):
                return "<TIMESTAMP>"
            return self.text(v)
        return v


def _path_forms(path: str) -> set[str]:
    forms = {path.rstrip("/\\") or path}
    try:
        forms.add(str(Path(path).resolve()))
    except OSError:
        pass
    return {f for f in forms if f and f not in ("/", "\\")}


# ---------------------------------------------------------------- cases

@dataclass
class Case:
    skill: str
    name: str
    dir: Path
    spec: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.skill}/{self.name}"


@dataclass
class Result:
    case: Case
    status: str  # pass | fail | updated | error
    exit_code: int | None = None
    diff: str = ""
    message: str = ""
    stderr: str = ""

    def to_json(self) -> dict:
        return {
            "case": self.case.id, "skill": self.case.skill, "status": self.status,
            "exit_code": self.exit_code, "expect_exit": self.case.spec.get("expect_exit"),
            "message": self.message, "diff": self.diff,
        }


def load_case(skill: str, case_dir: Path) -> Case:
    path = case_dir / "case.json"
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"{path}: {exc}") from exc
    if not isinstance(spec, dict):
        raise CaseError(f"{path}: must be a JSON object")
    if not isinstance(spec.get("script"), str) or not spec["script"]:
        raise CaseError(f"{path}: 'script' is required")
    if not isinstance(spec.get("expect_exit"), int):
        raise CaseError(f"{path}: 'expect_exit' (int) is required")
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise CaseError(f"{path}: 'args' must be a list of strings")
    if spec.get("cwd", "fixture") not in ("fixture", "repo"):
        raise CaseError(f"{path}: 'cwd' must be \"fixture\" or \"repo\"")
    if not isinstance(spec.get("setup", []), list):
        raise CaseError(f"{path}: 'setup' must be a list")
    tools = spec.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) and re.fullmatch(r"[\w.+-]+", t) for t in tools):
        raise CaseError(f"{path}: 'tools' must be a list of command names")
    return Case(skill, case_dir.name, case_dir, spec)


def discover(repo: Path, skills: list[str] | None = None) -> list[Case]:
    root = repo / GOLDEN_REL
    if not root.is_dir():
        return []
    cases = []
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if skills is not None and skill_dir.name not in skills:
            continue
        for case_dir in sorted(p for p in skill_dir.iterdir() if (p / "case.json").is_file()):
            cases.append(load_case(skill_dir.name, case_dir))
    return cases


def golden_skills(repo: Path) -> list[str]:
    root = repo / GOLDEN_REL
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


# ---------------------------------------------------------------- changed-from

def skills_from_paths(paths: list[str], known: list[str]) -> list[str] | None:
    """Skills touched by these repo-relative paths. None means run all."""
    hit: set[str] = set()
    for p in paths:
        p = p.strip().replace("\\", "/")
        if not p:
            continue
        if p in RUN_ALL_PATHS or any(p.startswith(x) for x in RUN_ALL_PATHS if x.endswith("/")):
            return None
        parts = p.split("/")
        if len(parts) >= 3 and parts[0] == "skills":
            hit.add(parts[1])
        elif len(parts) >= 4 and parts[0] == "evals" and parts[1] == "golden":
            hit.add(parts[2])
    return sorted(s for s in hit if s in known)


def changed_paths(repo: Path, ref: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{ref}...HEAD"],
        cwd=repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise CaseError(f"git diff --name-only {ref}...HEAD failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


# ---------------------------------------------------------------- running

def make_bin(bin_dir: Path, tools: list[str]) -> None:
    """Write wrapper scripts so PATH holds only python3, git, and any case `tools`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    targets = {"python3": sys.executable}
    for name in ["git", *tools]:
        found = shutil.which(name)
        if found:
            targets[name] = found
    for name, target in targets.items():
        wrapper = bin_dir / name
        wrapper.write_text(f'#!/bin/sh\nexec "{target}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)


def run_env(bin_dir: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if bin_dir is not None:
        env["PATH"] = str(bin_dir)
    env.update(GIT_ENV)
    env["TZ"] = "UTC"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    return env


def run_setup(case: Case, work: Path, env: dict[str, str]) -> None:
    for step in case.spec.get("setup", []):
        if isinstance(step, dict) and "copy" in step:
            src = (case.dir / step["copy"]).resolve()
            dst = (work / step.get("to", Path(step["copy"]).name)).resolve()
            if case.dir.resolve() not in src.parents or (work.resolve() != dst and work.resolve() not in dst.parents):
                raise CaseError(f"{case.id}: copy step must stay inside the case and fixture dirs")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            continue
        if not isinstance(step, list) or not step or not all(isinstance(a, str) for a in step):
            raise CaseError(f"{case.id}: bad setup step {step!r}")
        argv = [sys.executable if a == "python3" and i == 0 else a for i, a in enumerate(step)]
        proc = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=CASE_TIMEOUT)
        if proc.returncode != 0:
            raise CaseError(f"{case.id}: setup step {step!r} exited {proc.returncode}: {proc.stderr.strip()}")


def execute(case: Case, repo: Path) -> tuple[int, str, str, Normalizer]:
    """Run the case in a temp dir. Returns (exit code, stdout, stderr, normalizer)."""
    script = repo / "skills" / case.skill / "scripts" / case.spec["script"]
    if not script.is_file():
        raise CaseError(f"{case.id}: script not found: {script.relative_to(repo)}")
    tmp = tempfile.mkdtemp(prefix="parstack-golden-")
    try:
        work = Path(tmp) / "work"
        fixture = case.dir / "fixture"
        if fixture.is_dir():
            shutil.copytree(fixture, work)
        else:
            work.mkdir()
        bin_dir = Path(tmp) / "bin"
        make_bin(bin_dir, list(case.spec.get("tools", [])))
        env = run_env(bin_dir)
        run_setup(case, work, env)
        args = [a.replace("{fixture}", str(work)).replace("{repo}", str(repo)) for a in case.spec.get("args", [])]
        cwd = work if case.spec.get("cwd", "fixture") == "fixture" else repo
        try:
            proc = subprocess.run(
                [sys.executable, str(script), *args], cwd=cwd, env=env,
                capture_output=True, text=True, timeout=CASE_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise CaseError(f"{case.id}: timed out after {CASE_TIMEOUT}s") from exc
        return proc.returncode, proc.stdout, proc.stderr, Normalizer(str(work), repo)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render(case: Case, stdout: str, norm: Normalizer, text_mode: bool) -> str:
    if text_mode:
        return norm.text(stdout, iso=True)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CaseError(f"{case.id}: stdout is not JSON ({exc}); use expected.txt for text output") from exc
    return json.dumps(norm.value(data), indent=2, sort_keys=True) + "\n"


def run_case(case: Case, repo: Path, update: bool = False) -> Result:
    json_path, txt_path = case.dir / "expected.json", case.dir / "expected.txt"
    try:
        code, stdout, stderr, norm = execute(case, repo)
        if txt_path.is_file():
            text_mode = True
        elif json_path.is_file():
            text_mode = False
        else:
            try:
                json.loads(stdout)
                text_mode = False
            except json.JSONDecodeError:
                text_mode = True
        actual = render(case, stdout, norm, text_mode)
    except CaseError as exc:
        return Result(case, "error", message=str(exc))
    expect_exit = case.spec["expect_exit"]
    stderr_tail = norm.text(stderr)[-2000:]
    target = txt_path if text_mode else json_path
    if code != expect_exit:
        msg = f"exit {code}, expected {expect_exit}"
        if update:
            msg += " (golden not updated: fix the script or expect_exit first)"
        expected = target.read_text(encoding="utf-8") if target.is_file() else ""
        return Result(case, "fail", code, message=msg, stderr=stderr_tail,
                      diff=_diff(expected, actual, case, target.name))
    if update:
        old = target.read_text(encoding="utf-8") if target.is_file() else None
        if old == actual:
            return Result(case, "pass", code)
        target.write_text(actual, encoding="utf-8")
        return Result(case, "updated", code, message=f"wrote {target.name}")
    if not target.is_file():
        return Result(case, "fail", code, message=f"missing {target.name}; run with --update")
    expected = target.read_text(encoding="utf-8")
    if expected == actual:
        return Result(case, "pass", code)
    return Result(case, "fail", code, diff=_diff(expected, actual, case, target.name),
                  message="output differs", stderr=stderr_tail)


def _diff(expected: str, actual: str, case: Case, name: str) -> str:
    return "".join(difflib.unified_diff(
        expected.splitlines(keepends=True), actual.splitlines(keepends=True),
        fromfile=f"{case.id}/{name}", tofile=f"{case.id}/actual",
    ))


# ---------------------------------------------------------------- main

def select_skills(repo: Path, skills: list[str] | None, changed_from: str | None) -> tuple[list[str] | None, str]:
    """Return (skills to run or None for all, notice)."""
    known = golden_skills(repo)
    if skills:
        unknown = [s for s in skills if s not in known]
        if unknown:
            raise CaseError(f"no golden cases for: {', '.join(unknown)}")
    if changed_from is None:
        return skills, ""
    picked = skills_from_paths(changed_paths(repo, changed_from), known)
    if picked is None:
        return skills, f"run_golden.py or .github changed vs {changed_from}: running all"
    if skills:
        picked = [s for s in picked if s in skills]
    return picked, f"changed vs {changed_from}: {', '.join(picked) or 'none'}"


def main(argv: list[str] | None = None, repo: Path = REPO) -> int:
    ap = argparse.ArgumentParser(description="Run golden evals for the skill scripts.")
    ap.add_argument("--skill", action="append", metavar="NAME", help="only this skill (repeatable)")
    ap.add_argument("--changed-from", metavar="REF", help="only skills changed in REF...HEAD")
    ap.add_argument("--update", action="store_true", help="rewrite expected.json / expected.txt from current output")
    ap.add_argument("--json", action="store_true", help="print machine JSON")
    args = ap.parse_args(argv)

    try:
        skills, notice = select_skills(repo, args.skill, args.changed_from)
        if skills is not None and not skills:
            if args.json:
                print(json.dumps({"skills": [], "cases": [], "passed": 0, "failed": 0,
                                  "message": "no skills changed"}, indent=2))
            else:
                print("no skills changed")
            return 0
        cases = discover(repo, skills)
    except CaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if notice and not args.json:
        print(notice)
    results = [run_case(c, repo, args.update) for c in cases]
    failed = [r for r in results if r.status in ("fail", "error")]
    passed = [r for r in results if r.status in ("pass", "updated")]

    if args.json:
        print(json.dumps({
            "skills": sorted({c.skill for c in cases}),
            "cases": [r.to_json() for r in results],
            "passed": len(passed), "failed": len(failed), "message": notice,
        }, indent=2))
    else:
        for r in results:
            line = f"{r.status.upper():7} {r.case.id}"
            if r.message:
                line += f"  {r.message}"
            print(line)
            if r.status in ("fail", "error"):
                if r.diff:
                    print(r.diff.rstrip("\n"))
                if r.stderr.strip():
                    print("stderr:\n" + r.stderr.rstrip("\n"))
        print(f"{len(passed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
