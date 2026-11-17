import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "triage_checks.py")
FIXTURES = os.path.join(HERE, "fixtures")

_spec = importlib.util.spec_from_file_location("parstack_triage_checks", SCRIPT)
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

PY = shlex.quote(sys.executable)
# Check script committed into the test repo: reads results.json {test_id: bool} and fails listed ids.
CHECK_PY = """\
import json, sys
results = json.load(open("results.json"))
ids = sys.argv[1:] or sorted(results)
bad = [i for i in ids if not results.get(i, True)]
for i in bad:
    print("FAIL: " + i)
sys.exit(1 if bad else 0)
"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_main(argv, runner=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tc.main(argv, runner=runner)
    return code, out.getvalue(), err.getvalue()


class RepoCase(unittest.TestCase):
    """A temp git repo with a base commit on main and a head commit on a feature branch."""

    def make_repo(self, base_results, head_results):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        repo = os.path.join(self._tmp.name, "repo")
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        git(repo, "config", "commit.gpgsign", "false")
        with open(os.path.join(repo, "check.py"), "w") as fh:
            fh.write(CHECK_PY)
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write(".triage-rounds.json\n")
        self._write_results(repo, base_results)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        git(repo, "checkout", "-qb", "feature")
        self._write_results(repo, head_results)
        git(repo, "commit", "-qam", "head", "--allow-empty")
        return repo

    def _write_results(self, repo, results):
        with open(os.path.join(repo, "results.json"), "w") as fh:
            json.dump(results, fh)

    def triage(self, repo, *extra):
        argv = ["--repo", repo, "--json", *extra]
        code, out, err = run_main(argv)
        self.assertNotEqual(code, 2, err or out)
        return code, json.loads(out)

    def assert_no_worktrees_left(self, repo):
        out = subprocess.run(["git", "worktree", "list"], cwd=repo, stdout=subprocess.PIPE, text=True).stdout
        self.assertEqual(len(out.strip().splitlines()), 1, out)


class TriageIntegrationTest(RepoCase):
    def test_regression_single_command(self):
        repo = self.make_repo({"a": True}, {"a": False})
        code, rep = self.triage(repo, "--cmd", f"{PY} check.py", "--base", "main")
        self.assertEqual(code, 1)
        self.assertEqual(rep["status"], "blocking")
        self.assertEqual([(f["id"], f["label"]) for f in rep["failures"]], [("(command)", "regression")])
        self.assert_no_worktrees_left(repo)

    def test_already_broken_and_regression_by_regex(self):
        repo = self.make_repo({"a": False, "b": True}, {"a": False, "b": False})
        code, rep = self.triage(
            repo, "--cmd", f"{PY} check.py", "--base", "main", "--parse", r"regex:^FAIL: (?P<id>\S+)$",
        )
        labels = {f["id"]: f["label"] for f in rep["failures"]}
        self.assertEqual(labels, {"a": "already-broken", "b": "regression"})
        self.assertEqual(code, 1)
        self.assert_no_worktrees_left(repo)

    def test_only_already_broken_is_non_blocking(self):
        repo = self.make_repo({"a": False}, {"a": False})
        code, rep = self.triage(repo, "--cmd", f"{PY} check.py {{id}}", "--ids", "a", "--base", "main")
        self.assertEqual(code, 0)
        self.assertEqual(rep["status"], "non-blocking")
        self.assertEqual(rep["failures"][0]["label"], "already-broken")

    def test_per_id_template(self):
        repo = self.make_repo({"x": True, "y": True}, {"x": False, "y": True})
        code, rep = self.triage(repo, "--cmd", f"{PY} check.py {{id}}", "--ids", "x", "y", "--base", "main")
        self.assertEqual(code, 1)
        self.assertEqual([(f["id"], f["label"]) for f in rep["failures"]], [("x", "regression")])

    def test_flaky_on_head(self):
        repo = self.make_repo({"a": True}, {"a": True})
        counter = os.path.join(self._tmp.name, "counter")
        # Fails on odd-numbered invocations, passes on even.
        flaky = (
            f"{PY} -c \"import os,sys; p={counter!r}; n=int(open(p).read()) if os.path.exists(p) else 0; "
            "open(p,'w').write(str(n+1)); sys.exit(1 if n % 2 == 0 else 0)\""
        )
        code, rep = self.triage(repo, "--cmd", flaky, "--repeat", "2", "--base", "main")
        self.assertEqual(rep["failures"][0]["label"], "flaky")
        self.assertEqual(rep["failures"][0]["head"], ["fail", "pass"])
        self.assertEqual(code, 1)

    def test_runs_from_subdirectory_in_base_worktree(self):
        repo = self.make_repo({"a": True}, {"a": False})
        sub = os.path.join(repo, "pkg")
        os.makedirs(sub)
        with open(os.path.join(sub, "keep.txt"), "w") as fh:
            fh.write("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "pkg")
        git(repo, "branch", "-f", "main", "HEAD~2")
        # pkg/ does not exist on base; the base run falls back to the worktree root.
        code, rep = self.triage(sub, "--cmd", f"cd \"$(git rev-parse --show-toplevel)\" && {PY} check.py", "--base", "main")
        self.assertEqual(rep["failures"][0]["label"], "regression")
        self.assert_no_worktrees_left(repo)

    def test_clean_head_skips_base(self):
        repo = self.make_repo({"a": False}, {"a": True})
        code, rep = self.triage(repo, "--cmd", f"{PY} check.py", "--base", "main")
        self.assertEqual(code, 0)
        self.assertEqual(rep["status"], "clean")
        self.assertIn("base run: no failures on head", rep["skipped"])

    def test_default_base_is_merge_base_with_main(self):
        repo = self.make_repo({"a": True}, {"a": False})
        code, rep = self.triage(repo, "--cmd", f"{PY} check.py")
        self.assertEqual(rep["base_ref"], "merge-base(HEAD, main)")
        self.assertEqual(rep["failures"][0]["label"], "regression")

    def test_rounds_escalate_after_max(self):
        repo = self.make_repo({"a": True}, {"a": False})
        args = ["--cmd", f"{PY} check.py", "--base", "main", "--max-rounds", "2"]
        self.assertEqual(self.triage(repo, *args)[1]["round"], 1)
        self.assertEqual(self.triage(repo, *args)[1]["round"], 2)
        code, rep = self.triage(repo, *args)
        self.assertEqual(code, 1)
        self.assertEqual(rep["status"], "escalate")
        self.assertIn("Escalate to a human", rep["message"])
        self.assertEqual(rep["runs"], [])
        with open(os.path.join(repo, ".triage-rounds.json")) as fh:
            state = json.load(fh)
        self.assertEqual(list(state["rounds"].values())[0]["count"], 2)
        code, rep = self.triage(repo, *args, "--reset-rounds")
        self.assertEqual(rep["round"], 1)

    def test_green_run_resets_rounds(self):
        repo = self.make_repo({"a": True}, {"a": False})
        state = os.path.join(self._tmp.name, "state.json")
        args = ["--cmd", f"{PY} check.py", "--base", "main", "--state", state]
        self.triage(repo, *args)
        self._write_results(repo, {"a": True})
        code, rep = self.triage(repo, *args)
        self.assertEqual((code, rep["round"]), (0, 0))
        with open(state) as fh:
            self.assertEqual(json.load(fh)["rounds"], {})

    def test_human_summary(self):
        repo = self.make_repo({"a": True}, {"a": False})
        code, out, _ = run_main(["--repo", repo, "--cmd", f"{PY} check.py", "--base", "main", "--no-state"])
        self.assertEqual(code, 1)
        self.assertIn("status: blocking", out)
        self.assertIn("[regression] (command)", out)
        self.assertFalse(os.path.exists(os.path.join(repo, ".triage-rounds.json")))


class FakeRunner(tc.Runner):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.git_calls = []

    def shell(self, cmd, cwd, timeout):
        return self.outputs.pop(0)

    def git(self, args, cwd):
        self.git_calls.append(args)
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, cwd
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "feature"
        if args[0] == "rev-parse":
            return 0, "a" * 40
        if args[0] == "merge-base":
            return 0, "b" * 40
        if args[:2] == ["worktree", "add"]:
            os.makedirs(args[3], exist_ok=True)
        return 0, ""


class UnitTest(unittest.TestCase):
    def test_junit_failures(self):
        ids = tc.junit_failures(os.path.join(FIXTURES, "junit.xml"))
        self.assertEqual(ids, ["math.Add.adds_negative", "math.Div.by_zero"])

    def test_junit_ignores_stale_reports(self):
        self.assertIsNone(tc.junit_failures(os.path.join(FIXTURES, "junit.xml"), since=time.time() + 60))
        self.assertIsNone(tc.junit_failures(os.path.join(FIXTURES, "missing.xml")))

    def test_regex_failures(self):
        out = "ok a\nFAIL b\nFAIL c\nFAIL b\n"
        self.assertEqual(tc.regex_failures(r"^FAIL (\w+)$", out), ["b", "c"])
        self.assertEqual(tc.regex_failures(r"^FAIL (?P<id>\w+)$", out), ["b", "c"])

    def test_classify_labels(self):
        head = [{"failed": ["r", "k", "f"], "error": False}, {"failed": ["r", "k"], "error": False}]
        base = {"failed": ["k", "z"], "error": False}
        got = {f["id"]: f["label"] for f in tc.classify(head, base)}
        self.assertEqual(got, {"r": "regression", "k": "already-broken", "f": "flaky", "z": "fixed"})

    def test_classify_unclear_when_base_errors(self):
        head = [{"failed": ["t1"], "error": False}]
        base = {"failed": ["(command)"], "error": True}
        got = {f["id"]: f["label"] for f in tc.classify(head, base)}
        self.assertEqual(got["t1"], "unclear")

    def test_parse_command_failure_without_ids_is_error(self):
        runner = FakeRunner([(2, "SyntaxError somewhere\n")])
        r = tc.run_once(runner, "x", ".", ("regex", r"FAIL (\w+)"), [], None)
        self.assertTrue(r["error"])
        self.assertEqual(r["failed"], ["(command)"])

    def test_fake_runner_removes_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner([(1, "boom"), (0, "ok")])
            code, out, _ = run_main(["--repo", tmp, "--cmd", "check", "--no-state", "--json"], runner=runner)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["failures"][0]["label"], "regression")
            ops = [c[:2] for c in runner.git_calls]
            self.assertIn(["worktree", "add"], ops)
            self.assertIn(["worktree", "remove"], ops)
            self.assertLess(ops.index(["worktree", "add"]), ops.index(["worktree", "remove"]))

    def test_worktree_removed_when_command_raises(self):
        class Boom(FakeRunner):
            def shell(self, cmd, cwd, timeout):
                if "triage-base-" in cwd:
                    raise KeyboardInterrupt
                return 1, "fail"

        with tempfile.TemporaryDirectory() as tmp:
            runner = Boom([])
            with self.assertRaises(KeyboardInterrupt):
                run_main(["--repo", tmp, "--cmd", "check", "--no-state"], runner=runner)
            self.assertIn(["worktree", "remove"], [c[:2] for c in runner.git_calls])

    def test_usage_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = [
                ["--repo", tmp],
                ["--repo", tmp, "--cmd", "x", "--parse", "xml:foo"],
                ["--repo", tmp, "--cmd", "x", "--ids", "a"],
                ["--repo", tmp, "--cmd", "x", "--repeat", "0"],
            ]
            for argv in cases:
                code, _, _ = run_main(argv, runner=FakeRunner([]))
                self.assertEqual(code, 2, argv)
            code, _, err = run_main(["--repo", tmp, "--cmd", "x"])
            self.assertEqual(code, 2)
            self.assertIn("not a git repo", err)


if __name__ == "__main__":
    unittest.main()
