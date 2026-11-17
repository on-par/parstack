"""Unit tests for run_behavior.py. The `claude` subprocess is always mocked."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_behavior as rb  # noqa: E402

GOOD_SLICES = {
    "parent": {"id": "P1", "title": "Reset password",
               "criteria": [{"id": "AC1", "text": "User can request a reset link"}]},
    "slices": [{
        "id": "S1", "title": "Request a reset link", "value": "Locked-out users get back in",
        "scope": ["Reset form"], "files": ["src/auth/reset.ts"],
        "criteria": [{"id": "S1.1", "text": "Known email gets one link",
                      "proof": "test_sends_link_for_known_email", "covers": ["AC1"]}],
        "depends_on": [],
    }],
}


def assistant(*blocks):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}})


def tool(name, **inp):
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def text(s):
    return {"type": "text", "text": s}


def result(cost=0.02, duration_ms=12345, turns=4, final="done"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "total_cost_usd": cost, "duration_ms": duration_ms,
                       "num_turns": turns, "result": final})


def stream(*lines):
    return "\n".join([json.dumps({"type": "system", "subtype": "init"}), *lines]) + "\n"


class FakeRunner:
    """Answers `claude` calls with canned output; passes git and checks to the real subprocess."""

    def __init__(self, agent_stdout="", rubric_reply="PASS\nlooks right", agent_exc=None,
                 on_agent=None, git_diff=None):
        self.agent_stdout = agent_stdout
        self.rubric_reply = rubric_reply
        self.agent_exc = agent_exc
        self.on_agent = on_agent
        self.git_diff = git_diff
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if cmd[0] == "claude":
            if "stream-json" in cmd:
                if self.on_agent:
                    self.on_agent(Path(kw["cwd"]))
                if self.agent_exc:
                    raise self.agent_exc
                return subprocess.CompletedProcess(cmd, 0, self.agent_stdout, "")
            out = json.dumps({"type": "result", "result": self.rubric_reply, "total_cost_usd": 0.001})
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[:3] == ["git", "diff", "--name-only"] and self.git_diff is not None:
            return subprocess.CompletedProcess(cmd, 0, "\n".join(self.git_diff) + "\n", "")
        return subprocess.run(cmd, **kw)

    def claude_calls(self):
        return [c for c, _ in self.calls if c[0] == "claude"]


def make_case(tmp: Path, skill="slice-to-smallest-value", name="c1", **overrides) -> Path:
    case_dir = tmp / skill / name
    (case_dir / "fixture").mkdir(parents=True)
    (case_dir / "fixture" / "README.md").write_text("fixture\n")
    data = {"prompt": "split this up please", "expect_scripts": ["check_slices.py"], "max_turns": 5}
    data.update(overrides)
    (case_dir / "case.json").write_text(json.dumps(data))
    return case_dir


class ParseStreamTest(unittest.TestCase):
    def test_collects_skills_bash_text_and_result(self):
        out = stream(
            assistant(text("Looking."), tool("Skill", skill="parstack:slice-to-smallest-value")),
            "not json at all",
            assistant(tool("Bash", command='python3 "/x/scripts/check_slices.py" slices.json')),
            json.dumps({"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
            assistant(text("All slices pass.")),
            result(cost=0.05, duration_ms=2000, turns=6, final="Final answer"),
        )
        t = rb.parse_stream(out)
        self.assertEqual(t.skills, ["parstack:slice-to-smallest-value"])
        self.assertEqual(len(t.bash_commands), 1)
        self.assertEqual(t.cost_usd, 0.05)
        self.assertEqual(t.duration_ms, 2000)
        self.assertEqual(t.num_turns, 6)
        self.assertEqual(t.final_text, "Final answer")

    def test_final_text_falls_back_to_last_assistant_text(self):
        t = rb.parse_stream(stream(assistant(text("first")), assistant(text("last"))))
        self.assertIsNone(t.result)
        self.assertEqual(t.final_text, "last")
        self.assertIsNone(t.cost_usd)

    def test_legacy_cost_key(self):
        t = rb.parse_stream(json.dumps({"type": "result", "cost_usd": 0.3}))
        self.assertEqual(t.cost_usd, 0.3)


class SkillMatchTest(unittest.TestCase):
    def test_matches(self):
        for used in ["order-the-queue", "parstack:order-the-queue", "/order-the-queue", "other:order-the-queue"]:
            self.assertTrue(rb.skill_matches(used, "order-the-queue"), used)

    def test_no_match(self):
        for used in ["order-the-queue-extra", "parstack:check-issue-readiness", "order", ""]:
            self.assertFalse(rb.skill_matches(used, "order-the-queue"), used)

    def test_missing_scripts(self):
        cmds = ["python3 /a/scripts/check_slices.py s.json", "ls"]
        self.assertEqual(rb.missing_scripts(cmds, ["check_slices.py", "render.py"]), ["render.py"])
        self.assertEqual(rb.missing_scripts(cmds, []), [])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.case = rb.load_case(make_case(self.tmp, max_turns=9))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_agent_command(self):
        cmd = rb.claude_command(self.case, rb.Options(model="m1", max_budget_usd=0.5), Path("/repo"))
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertIn("--verbose", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        pairs = dict(zip(cmd, cmd[1:]))
        self.assertEqual(pairs["--plugin-dir"], "/repo")
        self.assertEqual(pairs["--model"], "m1")
        self.assertEqual(pairs["--output-format"], "stream-json")
        self.assertEqual(pairs["--max-turns"], "9")
        self.assertEqual(pairs["--permission-mode"], "acceptEdits")
        self.assertEqual(pairs["--max-budget-usd"], "0.5")
        i = cmd.index("--allowedTools")
        self.assertEqual(cmd[i + 1:], rb.ALLOWED_TOOLS)

    def test_no_budget_flag_by_default(self):
        self.assertNotIn("--max-budget-usd", rb.claude_command(self.case, rb.Options()))

    def test_default_model_from_env(self):
        old = os.environ.get("PARSTACK_EVAL_MODEL")
        try:
            os.environ["PARSTACK_EVAL_MODEL"] = "sonnet"
            self.assertEqual(rb.build_parser().parse_args([]).model, "sonnet")
            del os.environ["PARSTACK_EVAL_MODEL"]
            self.assertEqual(rb.build_parser().parse_args([]).model, "haiku")
        finally:
            if old is None:
                os.environ.pop("PARSTACK_EVAL_MODEL", None)
            else:
                os.environ["PARSTACK_EVAL_MODEL"] = old

    def test_rubric_model(self):
        cmd = rb.rubric_command(rb.Options(model="a", rubric_model="b"))
        self.assertEqual(dict(zip(cmd, cmd[1:]))["--model"], "b")
        self.assertNotIn("--plugin-dir", cmd)


class RunCaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def good_stream(self):
        return stream(
            assistant(tool("Skill", skill="parstack:slice-to-smallest-value")),
            assistant(tool("Bash", command="python3 x/scripts/check_slices.py slices.json")),
            result(),
        )

    def test_all_grades_pass_with_artifact_and_rubric(self):
        case = rb.load_case(make_case(
            self.tmp, rubric="slices are vertical",
            artifact={"path": "slices.json",
                      "check": ["python3", "{skill_dir}/scripts/check_slices.py", "{artifact}"],
                      "expect_exit": 0},
        ))
        seen = {}

        def agent(cwd):
            seen["git"] = (cwd / ".git").is_dir()
            seen["fixture"] = (cwd / "README.md").is_file()
            (cwd / "slices.json").write_text(json.dumps(GOOD_SLICES))

        fake = FakeRunner(self.good_stream(), on_agent=agent)
        rec = rb.run_case(case, rb.Options(), fake)
        self.assertTrue(seen["git"] and seen["fixture"])
        self.assertTrue(rec["passed"], rec)
        self.assertEqual(rec["grades"], {"skill_triggered": True, "scripts_invoked": True,
                                         "artifact_check": True, "rubric": True})
        self.assertAlmostEqual(rec["cost_usd"], 0.021)
        self.assertEqual(rec["duration_s"], 12.3)
        self.assertEqual(rec["num_turns"], 4)
        agent_call = next(kw for c, kw in fake.calls if "stream-json" in c)
        self.assertEqual(agent_call["input"], "split this up please")
        self.assertEqual(agent_call["timeout"], rb.DEFAULT_TIMEOUT)
        self.assertFalse(Path(agent_call["cwd"]).exists(), "temp dir should be removed")

    def test_artifact_check_fails_on_bad_plan(self):
        case = rb.load_case(make_case(
            self.tmp, artifact={"path": "slices.json",
                                "check": ["python3", "{skill_dir}/scripts/check_slices.py", "{artifact}"]},
        ))
        bad = json.loads(json.dumps(GOOD_SLICES))
        bad["slices"][0]["criteria"][0]["proof"] = "manual"
        fake = FakeRunner(self.good_stream(),
                          on_agent=lambda cwd: (cwd / "slices.json").write_text(json.dumps(bad)))
        rec = rb.run_case(case, rb.Options(), fake)
        self.assertFalse(rec["grades"]["artifact_check"])
        self.assertFalse(rec["passed"])

    def test_missing_artifact(self):
        case = rb.load_case(make_case(self.tmp, artifact={"path": "slices.json", "check": ["true"]}))
        rec = rb.run_case(case, rb.Options(), FakeRunner(self.good_stream()))
        self.assertFalse(rec["grades"]["artifact_check"])
        self.assertIn("not written", " ".join(rec["notes"]))

    def test_skill_not_triggered_and_script_missing(self):
        case = rb.load_case(make_case(self.tmp))
        out = stream(assistant(tool("Read", file_path="README.md")), result())
        rec = rb.run_case(case, rb.Options(), FakeRunner(out))
        self.assertFalse(rec["grades"]["skill_triggered"])
        self.assertFalse(rec["grades"]["scripts_invoked"])
        self.assertEqual(rec["missing_scripts"], ["check_slices.py"])
        self.assertIsNone(rec["grades"]["artifact_check"])
        self.assertFalse(rec["passed"])

    def test_rubric_fail_and_no_rubric(self):
        case = rb.load_case(make_case(self.tmp, rubric="must be good"))
        rec = rb.run_case(case, rb.Options(), FakeRunner(self.good_stream(), rubric_reply="**FAIL**\nnope"))
        self.assertFalse(rec["grades"]["rubric"])
        self.assertFalse(rec["passed"])

        fake = FakeRunner(self.good_stream(), rubric_reply="FAIL")
        rec = rb.run_case(case, rb.Options(rubric=False), fake)
        self.assertIsNone(rec["grades"]["rubric"])
        self.assertTrue(rec["passed"])
        self.assertEqual(len(fake.claude_calls()), 1)

    def test_timeout(self):
        case = rb.load_case(make_case(self.tmp, rubric="x"))
        partial = stream(assistant(tool("Skill", skill="slice-to-smallest-value")))
        exc = subprocess.TimeoutExpired(["claude"], 5, output=partial)
        fake = FakeRunner(agent_exc=exc)
        rec = rb.run_case(case, rb.Options(timeout=5), fake)
        self.assertFalse(rec["passed"])
        self.assertEqual(rec["error"], "timeout after 5s")
        self.assertTrue(rec["grades"]["skill_triggered"])
        self.assertEqual(len(fake.claude_calls()), 1, "no rubric call after a timeout")

    def test_setup_steps_run_with_placeholders(self):
        case_dir = make_case(self.tmp, setup=[
            ["git", "checkout", "-q", "-b", "feature"],
            ["cp", "{case_dir}/extra.txt", "extra.txt"],
            ["git", "add", "extra.txt"],
            ["git", "commit", "-q", "-m", "branch change"],
        ])
        (case_dir / "extra.txt").write_text("x\n")
        seen = {}

        def agent(cwd):
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=cwd,
                                    capture_output=True, text=True).stdout.strip()
            log = subprocess.run(["git", "log", "--format=%s"], cwd=cwd,
                                 capture_output=True, text=True).stdout.split()
            seen.update(branch=branch, log=log, extra=(cwd / "extra.txt").is_file())

        rb.run_case(rb.load_case(case_dir), rb.Options(), FakeRunner(self.good_stream(), on_agent=agent))
        self.assertEqual(seen["branch"], "feature")
        self.assertTrue(seen["extra"])
        self.assertEqual(seen["log"][-1], "fixture")

    def test_failed_setup_is_an_error(self):
        case = rb.load_case(make_case(self.tmp, setup=[["git", "checkout", "-q", "no-such-ref"]]))
        fake = FakeRunner(self.good_stream())
        rec = rb.run_case(case, rb.Options(), fake)
        self.assertFalse(rec["passed"])
        self.assertIn("setup step failed", rec["error"])
        self.assertEqual(fake.claude_calls(), [])


class RubricReplyTest(unittest.TestCase):
    def test_parse(self):
        self.assertTrue(rb.parse_rubric_reply("PASS\nfine")[0])
        self.assertFalse(rb.parse_rubric_reply("FAIL\nbad")[0])
        self.assertFalse(rb.parse_rubric_reply("")[0])
        ok, reply, cost = rb.parse_rubric_reply(json.dumps({"result": "\n## PASS - ok", "total_cost_usd": 0.01}))
        self.assertTrue(ok)
        self.assertEqual(cost, 0.01)


class SelectionTest(unittest.TestCase):
    AVAILABLE = {"order-the-queue", "slice-to-smallest-value", "triage-failing-checks"}

    def test_changed_skills(self):
        files = ["skills/order-the-queue/SKILL.md", "evals/behavior/triage-failing-checks/x/case.json",
                 "skills/set-up-work-triggers/scripts/a.py", "README.md", "evals/golden/order-the-queue/a"]
        self.assertEqual(rb.changed_skills(files, self.AVAILABLE),
                         {"order-the-queue", "triage-failing-checks"})

    def test_runner_change_runs_all(self):
        self.assertEqual(rb.changed_skills(["evals/run_behavior.py"], self.AVAILABLE), self.AVAILABLE)
        self.assertEqual(rb.changed_skills([".github/workflows/behavior-evals.yml"], self.AVAILABLE),
                         self.AVAILABLE)

    def test_nothing_changed(self):
        self.assertEqual(rb.changed_skills(["README.md", "tools/validate.py"], self.AVAILABLE), set())

    def test_max_per_skill_and_unknown_skill(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            make_case(tmp, name="a")
            make_case(tmp, name="b")
            make_case(tmp, skill="order-the-queue", name="a")
            cases = rb.discover_cases(tmp)
            self.assertEqual([c.id for c in rb.select_cases(cases, [], None, 1)],
                             ["order-the-queue/a", "slice-to-smallest-value/a"])
            self.assertEqual(len(rb.select_cases(cases, [], None, 0)), 3)
            self.assertEqual([c.id for c in rb.select_cases(cases, ["order-the-queue"], None, 1)],
                             ["order-the-queue/a"])
            with self.assertRaises(rb.UsageError):
                rb.select_cases(cases, ["nope"], None, 1)
        finally:
            shutil.rmtree(tmp)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        make_case(self.tmp, skill="order-the-queue", name="a", expect_scripts=["order_queue.py"])
        self.summary = self.tmp / "summary.md"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def main(self, argv, fake, which=lambda _: "/usr/bin/claude", env=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rb.main(argv + ["--summary-file", str(self.summary)], run=fake, which=which,
                           env={"ANTHROPIC_API_KEY": "k"} if env is None else env,
                           cases_dir=self.tmp)
        return code, buf.getvalue()

    def test_skip_when_claude_missing(self):
        fake = FakeRunner()
        code, out = self.main([], fake, which=lambda _: None)
        self.assertEqual(code, 0)
        self.assertIn("::notice::", out)
        self.assertIn("claude not found", self.summary.read_text())
        self.assertEqual(fake.claude_calls(), [])

    def passing_run(self):
        return FakeRunner(stream(
            assistant(tool("Skill", skill="order-the-queue"),
                      tool("Bash", command="python3 s/order_queue.py --file issues.json")),
            result()))

    def test_skip_when_auth_required_and_none_set(self):
        for env in ({}, {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "  "}):
            with self.subTest(env=env):
                fake = FakeRunner()
                code, out = self.main(["--require-auth"], fake, env=env)
                self.assertEqual(code, 0)
                self.assertIn("::notice::", out)
                self.assertIn("neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY", out)
                self.assertEqual(fake.claude_calls(), [])

    def test_require_auth_runs_with_oauth_token(self):
        fake = self.passing_run()
        code, _ = self.main(["--require-auth"], fake, env={"CLAUDE_CODE_OAUTH_TOKEN": "t"})
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.claude_calls()), 1)

    def test_require_auth_runs_with_api_key(self):
        fake = self.passing_run()
        code, _ = self.main(["--require-auth"], fake, env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.claude_calls()), 1)

    def test_require_api_key_is_hidden_alias(self):
        fake = FakeRunner()
        code, out = self.main(["--require-api-key"], fake, env={"ANTHROPIC_API_KEY": ""})
        self.assertEqual(code, 0)
        self.assertIn("neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY", out)
        self.assertEqual(fake.claude_calls(), [])
        self.assertNotIn("--require-api-key", rb.build_parser().format_help())
        self.assertIn("--require-auth", rb.build_parser().format_help())

    def test_no_auth_without_flag_still_runs(self):
        fake = self.passing_run()
        code, _ = self.main([], fake, env={})
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.claude_calls()), 1)

    def test_changed_from_nothing_changed(self):
        fake = FakeRunner(git_diff=["README.md"])
        code, out = self.main(["--changed-from", "origin/main"], fake)
        self.assertEqual(code, 0)
        self.assertIn("no skills changed", out)
        self.assertEqual(fake.claude_calls(), [])

    def test_changed_from_runs_changed_skill_and_fails(self):
        fake = FakeRunner(stream(result()), git_diff=["skills/order-the-queue/SKILL.md"])
        code, out = self.main(["--changed-from", "origin/main", "--json"], fake)
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["cases"][0]["id"], "order-the-queue/a")
        summary = self.summary.read_text()
        self.assertIn("| order-the-queue/a | FAIL |", summary)
        self.assertIn("skill not triggered", summary)

    def test_unknown_skill_is_usage_error(self):
        code, _ = self.main(["--skill", "nope"], FakeRunner())
        self.assertEqual(code, 2)


class SummaryTest(unittest.TestCase):
    def record(self, **kw):
        rec = {"id": "s/c", "skill": "s", "passed": True,
               "grades": {"skill_triggered": True, "scripts_invoked": True,
                          "artifact_check": None, "rubric": True},
               "missing_scripts": [], "skills_used": ["s"], "cost_usd": 0.0123,
               "duration_s": 8.5, "num_turns": 3, "notes": [], "error": None}
        rec.update(kw)
        return rec

    def test_markdown(self):
        bad = self.record(id="s/d", passed=False, error="timeout after 300s", cost_usd=None,
                          grades={"skill_triggered": False, "scripts_invoked": False,
                                  "artifact_check": False, "rubric": None},
                          missing_scripts=["x.py"])
        md = rb.render_summary([self.record(), bad], "haiku")
        self.assertIn("## Behavior evals", md)
        self.assertIn("Model `haiku`. 1/2 passed. Cost $0.012.", md)
        self.assertIn("| s/c | pass | pass | - | pass | PASS | $0.012 | 8.5 | 3 |", md)
        self.assertIn("| s/d | FAIL | FAIL | FAIL | - | FAIL | - | 8.5 | 3 |", md)
        self.assertIn("- `s/d`: timeout after 300s; scripts not run: x.py; skill not triggered", md)

    def test_table(self):
        out = rb.render_table([self.record()])
        self.assertIn("case", out.splitlines()[0])
        self.assertIn("behavior evals: 1/1 passed, cost $0.012", out)


class RealCasesTest(unittest.TestCase):
    """The shipped cases are well-formed and their prompts do not name the skill."""

    def test_cases(self):
        cases = rb.discover_cases()
        required = {"slice-to-smallest-value", "check-issue-readiness", "order-the-queue",
                    "assess-agent-readiness", "triage-failing-checks"}
        self.assertLessEqual(required, set(cases))
        for skill, items in cases.items():
            for case in items:
                skill_dir = rb.SKILLS_DIR / case.expect_skill
                self.assertTrue((skill_dir / "SKILL.md").is_file(), case.id)
                for script in case.expect_scripts:
                    self.assertTrue((skill_dir / "scripts" / script).is_file(), f"{case.id}: {script}")
                prompt = case.prompt.lower()
                self.assertNotIn(case.expect_skill, prompt, case.id)
                self.assertNotIn(case.expect_skill.replace("-", " "), prompt, case.id)
                if case.artifact:
                    check = " ".join(case.artifact["check"]).replace("{skill_dir}", str(skill_dir))
                    for part in check.split():
                        if part.endswith(".py"):
                            self.assertTrue(Path(part).is_file(), f"{case.id}: {part}")


if __name__ == "__main__":
    unittest.main()
