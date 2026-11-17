#!/usr/bin/env python3
"""Behavior evals: run real agent sessions against skill cases and grade them.

Opt-in. Each case costs model calls. Never run in the default CI job.

Case layout:

    evals/behavior/<skill>/<case>/case.json
    evals/behavior/<skill>/<case>/fixture/      (optional sample repo)

case.json:

    {
      "prompt": "a real user request that does not name the skill",
      "expect_skill": "<skill>",                 (default: the <skill> dir name)
      "expect_scripts": ["check_slices.py"],
      "artifact": {                              (optional)
        "path": "slices.json",
        "check": ["python3", "{skill_dir}/scripts/check_slices.py", "{artifact}"],
        "expect_exit": 0
      },
      "rubric": "one line",                      (optional)
      "max_turns": 15,
      "setup": [["git", "checkout", "-q", "-b", "feature"]]   (optional)
    }

Per case the runner copies the fixture to a temp dir, runs `git init` and
commits it on `main`, runs any `setup` commands (argv lists; `{case_dir}` and
`{workdir}` are substituted), then runs `claude -p` there with the prompt on
stdin and grades the stream-json transcript:

- skill_triggered: a Skill tool use named <skill> or <namespace>:<skill>
- scripts_invoked: every expected script name appears in a Bash command
- artifact_check: the check command exits with expect_exit
- rubric: a second model call answers PASS or FAIL (skip with --no-rubric)

Exit 0 when every case passes or the run is skipped, 1 when any case fails,
2 on usage error. The runner never reads or copies credential files.
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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = REPO_ROOT / "evals" / "behavior"
SKILLS_DIR = REPO_ROOT / "skills"

DEFAULT_MODEL = "haiku"
DEFAULT_MAX_TURNS = 15
DEFAULT_TIMEOUT = 300
ARTIFACT_TIMEOUT = 120
RUBRIC_TIMEOUT = 120
RUBRIC_CHARS = 20000
ALLOWED_TOOLS = ["Bash(python3:*)", "Read", "Write", "Edit", "Glob", "Grep", "Skill"]
# Changes to these files rerun every case.
RUN_ALL_FILES = {"evals/run_behavior.py", ".github/workflows/behavior-evals.yml"}

GIT_ENV = {
    "GIT_AUTHOR_NAME": "parstack-eval",
    "GIT_AUTHOR_EMAIL": "eval@example.invalid",
    "GIT_COMMITTER_NAME": "parstack-eval",
    "GIT_COMMITTER_EMAIL": "eval@example.invalid",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "commit.gpgsign",
    "GIT_CONFIG_VALUE_0": "false",
}

Runner = Callable[..., subprocess.CompletedProcess]


class UsageError(Exception):
    pass


# --- cases -------------------------------------------------------------------

@dataclass
class Case:
    skill: str
    name: str
    dir: Path
    prompt: str
    expect_skill: str
    expect_scripts: list[str]
    artifact: dict[str, Any] | None
    rubric: str | None
    max_turns: int
    setup: list[list[str]]

    @property
    def id(self) -> str:
        return f"{self.skill}/{self.name}"


def load_case(case_dir: Path) -> Case:
    path = case_dir / "case.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError(f"{path}: must be a JSON object")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise UsageError(f"{path}: prompt is required")
    scripts = data.get("expect_scripts", [])
    if not isinstance(scripts, list) or not all(isinstance(s, str) for s in scripts):
        raise UsageError(f"{path}: expect_scripts must be a list of strings")
    artifact = data.get("artifact")
    if artifact is not None:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("path"), str)
            or not isinstance(artifact.get("check"), list)
            or not artifact["check"]
        ):
            raise UsageError(f"{path}: artifact needs path (string) and check (argv list)")
    setup = data.get("setup", [])
    if not isinstance(setup, list) or not all(
        isinstance(step, list) and step and all(isinstance(a, str) for a in step) for step in setup
    ):
        raise UsageError(f"{path}: setup must be a list of argv lists")
    rubric = data.get("rubric")
    return Case(
        skill=case_dir.parent.name,
        name=case_dir.name,
        dir=case_dir,
        prompt=prompt,
        expect_skill=data.get("expect_skill") or case_dir.parent.name,
        expect_scripts=scripts,
        artifact=artifact,
        rubric=rubric if isinstance(rubric, str) and rubric.strip() else None,
        max_turns=int(data.get("max_turns", DEFAULT_MAX_TURNS)),
        setup=setup,
    )


def discover_cases(cases_dir: Path = CASES_DIR) -> dict[str, list[Case]]:
    found: dict[str, list[Case]] = {}
    if not cases_dir.is_dir():
        return found
    for skill_dir in sorted(p for p in cases_dir.iterdir() if p.is_dir()):
        cases = [load_case(c) for c in sorted(skill_dir.iterdir()) if (c / "case.json").is_file()]
        if cases:
            found[skill_dir.name] = cases
    return found


def changed_skills(files: list[str], available: set[str]) -> set[str]:
    """Skills with behavior cases whose skill or case files changed."""
    if any(f in RUN_ALL_FILES for f in files):
        return set(available)
    hit: set[str] = set()
    for f in files:
        parts = f.split("/")
        if len(parts) >= 3 and parts[0] == "skills":
            hit.add(parts[1])
        elif len(parts) >= 4 and parts[:2] == ["evals", "behavior"]:
            hit.add(parts[2])
    return hit & available


def git_changed_files(ref: str, run: Runner, cwd: Path = REPO_ROOT) -> list[str]:
    proc = run(
        ["git", "diff", "--name-only", f"{ref}...HEAD"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise UsageError(f"git diff against {ref} failed: {(proc.stderr or '').strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def select_cases(
    all_cases: dict[str, list[Case]],
    skills: list[str] | None,
    changed: set[str] | None,
    max_per_skill: int,
) -> list[Case]:
    names = set(all_cases)
    if skills:
        unknown = sorted(set(skills) - names)
        if unknown:
            raise UsageError(
                f"no behavior cases for: {', '.join(unknown)} (have: {', '.join(sorted(names))})"
            )
        names &= set(skills)
    if changed is not None:
        names &= changed
    picked: list[Case] = []
    for skill in sorted(names):
        picked.extend(all_cases[skill][:max_per_skill] if max_per_skill > 0 else all_cases[skill])
    return picked


# --- stream-json parsing -----------------------------------------------------

@dataclass
class Transcript:
    skills: list[str] = field(default_factory=list)
    bash_commands: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None

    @property
    def final_text(self) -> str:
        if self.result and isinstance(self.result.get("result"), str) and self.result["result"].strip():
            return self.result["result"]
        return self.texts[-1] if self.texts else ""

    @property
    def cost_usd(self) -> float | None:
        if not self.result:
            return None
        for key in ("total_cost_usd", "cost_usd"):
            value = self.result.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    @property
    def duration_ms(self) -> int | None:
        value = (self.result or {}).get("duration_ms")
        return int(value) if isinstance(value, (int, float)) else None

    @property
    def num_turns(self) -> int | None:
        value = (self.result or {}).get("num_turns")
        return int(value) if isinstance(value, int) else None


def parse_stream(text: str) -> Transcript:
    t = Transcript()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "result":
            t.result = event
            continue
        if kind != "assistant":
            continue
        content = (event.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                if block["text"].strip():
                    t.texts.append(block["text"])
            elif block.get("type") == "tool_use":
                name = block.get("name")
                inp = block.get("input") or {}
                if not isinstance(inp, dict):
                    continue
                if name == "Skill":
                    value = inp.get("skill") or inp.get("command") or inp.get("name")
                    if isinstance(value, str):
                        t.skills.append(value)
                elif name == "Bash" and isinstance(inp.get("command"), str):
                    t.bash_commands.append(inp["command"])
    return t


def skill_matches(used: str, expected: str) -> bool:
    """True for `expected`, `/expected`, or `<namespace>:expected`."""
    name = used.strip().lstrip("/")
    return name == expected or name.rsplit(":", 1)[-1] == expected


def missing_scripts(commands: list[str], expected: list[str]) -> list[str]:
    return [s for s in expected if not any(s in c for c in commands)]


# --- running -----------------------------------------------------------------

@dataclass
class Options:
    model: str = DEFAULT_MODEL
    rubric_model: str | None = None
    timeout: int = DEFAULT_TIMEOUT
    max_budget_usd: float | None = None
    rubric: bool = True


def claude_command(case: Case, opts: Options, plugin_dir: Path = REPO_ROOT) -> list[str]:
    cmd = [
        "claude", "-p",
        # Load only project settings so plugins enabled in the user's own
        # settings do not compete with the plugin under test.
        "--setting-sources", "project",
        "--plugin-dir", str(plugin_dir),
        "--model", opts.model,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(case.max_turns),
        "--permission-mode", "acceptEdits",
    ]
    if opts.max_budget_usd is not None:
        cmd += ["--max-budget-usd", f"{opts.max_budget_usd:g}"]
    cmd += ["--allowedTools", *ALLOWED_TOOLS]
    return cmd


def rubric_command(opts: Options) -> list[str]:
    cmd = ["claude", "-p", "--setting-sources", "project",
           "--model", opts.rubric_model or opts.model,
           "--output-format", "json", "--max-turns", "1"]
    if opts.max_budget_usd is not None:
        cmd += ["--max-budget-usd", f"{opts.max_budget_usd:g}"]
    return cmd


def _sub(arg: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        arg = arg.replace("{" + key + "}", value)
    return arg


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def prepare_workdir(case: Case, workdir: Path, run: Runner) -> None:
    fixture = case.dir / "fixture"
    if fixture.is_dir():
        shutil.copytree(
            fixture, workdir, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
    env = {**os.environ, **GIT_ENV}
    steps = [
        ["git", "-c", "init.defaultBranch=main", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "--allow-empty", "-m", "fixture"],
    ]
    values = {"case_dir": str(case.dir), "workdir": str(workdir)}
    steps += [[_sub(a, values) for a in step] for step in case.setup]
    for step in steps:
        proc = run(step, cwd=str(workdir), env=env, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"setup step failed: {' '.join(step)}: {_text(proc.stderr).strip()}")


def check_artifact(case: Case, workdir: Path, run: Runner) -> tuple[bool, str]:
    art = case.artifact or {}
    path = workdir / art["path"]
    if not path.is_file():
        return False, f"artifact {art['path']} not written"
    values = {
        "skill_dir": str(SKILLS_DIR / case.expect_skill),
        "artifact": str(path),
        "workdir": str(workdir),
    }
    cmd = [_sub(a, values) for a in art["check"]]
    if cmd[0] == "python3":
        cmd[0] = sys.executable
    want = int(art.get("expect_exit", 0))
    try:
        proc = run(cmd, cwd=str(workdir), capture_output=True, text=True, timeout=ARTIFACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"artifact check timed out after {ARTIFACT_TIMEOUT}s"
    if proc.returncode == want:
        return True, ""
    tail = _text(proc.stdout).strip().splitlines()[-5:]
    return False, f"artifact check exited {proc.returncode}, want {want}: " + " | ".join(tail)


def parse_rubric_reply(stdout: str) -> tuple[bool, str, float | None]:
    """Return (passed, reply text, cost) from the rubric call's output."""
    reply, cost = stdout, None
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            reply = _text(data.get("result"))
            for key in ("total_cost_usd", "cost_usd"):
                if isinstance(data.get(key), (int, float)):
                    cost = float(data[key])
                    break
    except json.JSONDecodeError:
        pass
    first = next((ln.strip() for ln in reply.splitlines() if ln.strip()), "")
    first = re.sub(r"^[*_#`\s]+", "", first).upper()
    return first.startswith("PASS"), reply.strip(), cost


def rubric_prompt(case: Case, final_text: str, artifact_text: str | None) -> str:
    parts = [
        "You are grading an AI agent's work on a task. Judge only against the rubric.",
        f"Rubric: {case.rubric}",
        "Answer PASS or FAIL on the first line, then one sentence explaining why.",
        f"--- task given to the agent ---\n{case.prompt}",
        f"--- agent's final answer ---\n{final_text[:RUBRIC_CHARS] or '(empty)'}",
    ]
    if artifact_text is not None:
        parts.append(f"--- artifact {case.artifact['path']} ---\n{artifact_text[:RUBRIC_CHARS]}")
    return "\n\n".join(parts) + "\n"


def run_rubric(case: Case, workdir: Path, transcript: Transcript, opts: Options,
               run: Runner) -> tuple[bool, str, float | None]:
    artifact_text = None
    if case.artifact:
        path = workdir / case.artifact["path"]
        if path.is_file():
            artifact_text = path.read_text(encoding="utf-8", errors="replace")
    prompt = rubric_prompt(case, transcript.final_text, artifact_text)
    try:
        proc = run(rubric_command(opts), input=prompt, cwd=str(workdir),
                   capture_output=True, text=True, timeout=RUBRIC_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"rubric call timed out after {RUBRIC_TIMEOUT}s", None
    if proc.returncode != 0:
        return False, f"rubric call exited {proc.returncode}: {_text(proc.stderr).strip()[:200]}", None
    return parse_rubric_reply(_text(proc.stdout))


def run_case(case: Case, opts: Options, run: Runner = subprocess.run) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": case.id, "skill": case.expect_skill, "passed": False,
        "grades": {"skill_triggered": False, "scripts_invoked": False,
                   "artifact_check": None, "rubric": None},
        "missing_scripts": list(case.expect_scripts), "skills_used": [],
        "cost_usd": None, "duration_s": None, "num_turns": None,
        "exit_code": None, "result_subtype": None, "notes": [], "error": None,
    }
    workdir = Path(tempfile.mkdtemp(prefix="parstack-eval-"))
    started = time.monotonic()
    try:
        prepare_workdir(case, workdir, run)
        timed_out = False
        try:
            proc = run(claude_command(case, opts), input=case.prompt, cwd=str(workdir),
                       capture_output=True, text=True, timeout=opts.timeout)
            stdout, record["exit_code"] = _text(proc.stdout), proc.returncode
            if proc.returncode != 0:
                err = _text(proc.stderr).strip()
                record["notes"].append(f"claude exited {proc.returncode}" + (f": {err[:300]}" if err else ""))
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = _text(exc.stdout)
            record["error"] = f"timeout after {opts.timeout}s"

        t = parse_stream(stdout)
        grades = record["grades"]
        record["skills_used"] = t.skills
        grades["skill_triggered"] = any(skill_matches(s, case.expect_skill) for s in t.skills)
        record["missing_scripts"] = missing_scripts(t.bash_commands, case.expect_scripts)
        grades["scripts_invoked"] = not record["missing_scripts"]
        record["cost_usd"] = t.cost_usd
        record["num_turns"] = t.num_turns
        record["result_subtype"] = (t.result or {}).get("subtype")
        if t.duration_ms is not None:
            record["duration_s"] = round(t.duration_ms / 1000, 1)
        if t.result is None and not timed_out:
            record["notes"].append("no result event in stream")

        if case.artifact:
            ok, note = check_artifact(case, workdir, run)
            grades["artifact_check"] = ok
            if note:
                record["notes"].append(note)
        if case.rubric and opts.rubric and not timed_out:
            ok, reply, cost = run_rubric(case, workdir, t, opts, run)
            grades["rubric"] = ok
            record["rubric_reply"] = reply[:500]
            if cost is not None:
                record["cost_usd"] = (record["cost_usd"] or 0.0) + cost
        elif case.rubric and opts.rubric:
            grades["rubric"] = False

        record["passed"] = not timed_out and all(v is not False for v in grades.values())
    except (RuntimeError, OSError) as exc:
        record["error"] = str(exc)
    finally:
        if record["duration_s"] is None:
            record["duration_s"] = round(time.monotonic() - started, 1)
        shutil.rmtree(workdir, ignore_errors=True)
    return record


# --- output ------------------------------------------------------------------

def _mark(value: bool | None) -> str:
    return "-" if value is None else ("pass" if value else "FAIL")


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:.3f}"


COLUMNS = ["case", "skill", "scripts", "artifact", "rubric", "result", "cost", "secs", "turns"]


def _rows(results: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for r in results:
        g = r["grades"]
        rows.append([
            r["id"], _mark(g["skill_triggered"]), _mark(g["scripts_invoked"]),
            _mark(g["artifact_check"]), _mark(g["rubric"]),
            "PASS" if r["passed"] else "FAIL", _money(r["cost_usd"]),
            "-" if r["duration_s"] is None else f"{r['duration_s']:g}",
            "-" if r["num_turns"] is None else str(r["num_turns"]),
        ])
    return rows


def total_cost(results: list[dict[str, Any]]) -> float:
    return sum(r["cost_usd"] or 0.0 for r in results)


def render_table(results: list[dict[str, Any]]) -> str:
    rows = [COLUMNS] + _rows(results)
    widths = [max(len(row[i]) for row in rows) for i in range(len(COLUMNS))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows]
    for r in results:
        details = list(r["notes"])
        if r["error"]:
            details.insert(0, r["error"])
        if r["missing_scripts"]:
            details.append("scripts not run: " + ", ".join(r["missing_scripts"]))
        if not r["grades"]["skill_triggered"]:
            details.append("skills used: " + (", ".join(r["skills_used"]) or "none"))
        if not r["passed"] and details:
            lines.append(f"{r['id']}: " + "; ".join(details))
    passed = sum(1 for r in results if r["passed"])
    lines.append(f"behavior evals: {passed}/{len(results)} passed, cost {_money(total_cost(results))}")
    return "\n".join(lines)


def render_summary(results: list[dict[str, Any]], model: str) -> str:
    passed = sum(1 for r in results if r["passed"])
    out = [
        "## Behavior evals", "",
        f"Model `{model}`. {passed}/{len(results)} passed. Cost {_money(total_cost(results))}.", "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    for row in _rows(results):
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
    failed = [r for r in results if not r["passed"]]
    if failed:
        out += ["", "Failures:", ""]
        for r in failed:
            why = [r["error"]] if r["error"] else []
            why += r["notes"]
            if r["missing_scripts"]:
                why.append("scripts not run: " + ", ".join(r["missing_scripts"]))
            if not r["grades"]["skill_triggered"]:
                why.append("skill not triggered")
            if r["grades"]["rubric"] is False:
                reply = " ".join((r.get("rubric_reply") or "").split())
                why.append(("rubric: " + reply[:300]) if reply else "rubric failed")
            out.append(f"- `{r['id']}`: " + "; ".join(why))
    return "\n".join(out) + "\n"


def write_summary(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


# --- main --------------------------------------------------------------------

def skip_reason(require_api_key: bool, which: Callable[[str], str | None] = shutil.which,
                env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    if which("claude") is None:
        return "claude not found on PATH"
    if require_api_key and not env.get("ANTHROPIC_API_KEY", "").strip():
        return "ANTHROPIC_API_KEY is not set"
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run opt-in behavior evals (real model calls).")
    p.add_argument("--skill", action="append", default=[], metavar="NAME",
                   help="run only this skill's cases (repeatable)")
    p.add_argument("--changed-from", metavar="REF",
                   help="run only skills whose skill or case files changed vs REF")
    p.add_argument("--model", default=os.environ.get("PARSTACK_EVAL_MODEL") or DEFAULT_MODEL,
                   help="model for the agent run (default: $PARSTACK_EVAL_MODEL or haiku)")
    p.add_argument("--max-cases-per-skill", type=int, default=1, metavar="N",
                   help="cases per skill, 0 for all (default 1)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, metavar="SEC",
                   help="seconds per agent run (default 300)")
    p.add_argument("--max-budget-usd", type=float, metavar="X",
                   help="spend cap per model call, passed to claude")
    p.add_argument("--rubric-model", metavar="M", help="model for rubric grading (default: --model)")
    p.add_argument("--no-rubric", action="store_true", help="skip rubric grading")
    p.add_argument("--require-api-key", action="store_true",
                   help="skip (exit 0) when ANTHROPIC_API_KEY is empty")
    p.add_argument("--json", action="store_true", help="print machine JSON")
    p.add_argument("--summary-file", metavar="PATH", help="append a markdown summary here")
    return p


def main(argv: list[str] | None = None, run: Runner = subprocess.run,
         which: Callable[[str], str | None] = shutil.which,
         env: dict[str, str] | None = None, cases_dir: Path = CASES_DIR) -> int:
    args = build_parser().parse_args(argv)
    opts = Options(model=args.model, rubric_model=args.rubric_model, timeout=args.timeout,
                   max_budget_usd=args.max_budget_usd, rubric=not args.no_rubric)

    def finish(results: list[dict[str, Any]] | None, notice: str | None = None,
               gh_notice: bool = False) -> int:
        if notice is not None:
            if args.json:
                print(json.dumps({"skipped": True, "reason": notice, "cases": []}, indent=2))
            else:
                print(f"::notice::behavior evals {notice}" if gh_notice else notice)
            write_summary(args.summary_file, f"Behavior evals: {notice}.\n")
            return 0
        assert results is not None
        failed = sum(1 for r in results if not r["passed"])
        if args.json:
            print(json.dumps({
                "skipped": False, "model": opts.model, "cases": results,
                "passed": len(results) - failed, "failed": failed,
                "total_cost_usd": round(total_cost(results), 4),
            }, indent=2))
        else:
            print(render_table(results))
        write_summary(args.summary_file, render_summary(results, opts.model))
        return 1 if failed else 0

    try:
        all_cases = discover_cases(cases_dir)
        changed = None
        if args.changed_from:
            changed = changed_skills(git_changed_files(args.changed_from, run), set(all_cases))
            if not changed:
                return finish(None, "no skills changed")
        cases = select_cases(all_cases, args.skill, changed, args.max_cases_per_skill)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not cases:
        return finish(None, "no behavior cases selected")

    reason = skip_reason(args.require_api_key, which, env)
    if reason:
        return finish(None, f"skipped: {reason}", gh_notice=True)

    results = []
    for case in cases:
        if not args.json:
            print(f"running {case.id} ...", file=sys.stderr, flush=True)
        results.append(run_case(case, opts, run))
    return finish(results)


if __name__ == "__main__":
    sys.exit(main())
