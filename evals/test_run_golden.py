"""Unit tests for evals/run_golden.py. Run: python3 -m unittest discover -s evals -p 'test_*.py'"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_golden as rg  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, **rg.GIT_ENV)
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout


def run_main(argv: list[str], repo: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = rg.main(argv, repo=repo)
    return code, out.getvalue()


class NormalizerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="golden-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = "/home/someone/src/parstack"
        self.n = rg.Normalizer(self.tmp, self.repo)

    def test_fixture_dir_and_realpath_become_tmp(self):
        real = str(Path(self.tmp).resolve())
        self.assertEqual(self.n.text(f"{self.tmp}/a.ts"), "<TMP>/a.ts")
        self.assertEqual(self.n.text(f"{real}/a.ts"), "<TMP>/a.ts")

    def test_other_temp_paths_become_tmp(self):
        other = os.path.join(tempfile.gettempdir(), "wt-abc123", "src")
        self.assertEqual(self.n.text(f"worktree at {other}"), "worktree at <TMP>/src")

    def test_repo_root_becomes_repo(self):
        self.assertEqual(self.n.text("/home/someone/src/parstack/skills/x"), "<REPO>/skills/x")

    def test_full_sha_anywhere(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(self.n.text(f"head {sha} ok"), "head <SHA> ok")

    def test_short_sha_only_under_sha_keys(self):
        data = self.n.value({"head": "abc1234", "title": "abc1234"})
        self.assertEqual(data, {"head": "<SHA>", "title": "abc1234"})

    def test_timing_keys_zeroed_others_kept(self):
        data = self.n.value({
            "duration_s": 1.23, "elapsed": 9, "timings": {"verify": 2.5}, "durations": [1.0, 2.0],
            "max_rounds": 3, "stale_days": 30, "score": 72, "ok": True,
        })
        self.assertEqual(data, {
            "duration_s": 0, "elapsed": 0, "timings": {"verify": 0}, "durations": [0, 0],
            "max_rounds": 3, "stale_days": 30, "score": 72, "ok": True,
        })

    def test_timestamps_only_under_timestamp_keys(self):
        data = self.n.value({"at": "2026-11-17T10:00:00Z", "created_at": "2026-11-17 10:00",
                             "updatedAt": "2026-11-10T00:00:00Z", "note": "2026-11-17T10:00:00Z"})
        self.assertEqual(data, {"at": "<TIMESTAMP>", "created_at": "<TIMESTAMP>",
                                "updatedAt": "2026-11-10T00:00:00Z", "note": "2026-11-17T10:00:00Z"})

    def test_text_mode_iso(self):
        self.assertEqual(self.n.text("ran at 2026-11-17T10:00:00Z", iso=True), "ran at <TIMESTAMP>")


class ChangedPathsTest(unittest.TestCase):
    KNOWN = ["order-the-queue", "slice-to-smallest-value"]

    def test_skill_and_golden_paths(self):
        paths = ["skills/order-the-queue/scripts/order_queue.py",
                 "evals/golden/slice-to-smallest-value/clean-slices/case.json"]
        self.assertEqual(rg.skills_from_paths(paths, self.KNOWN), ["order-the-queue", "slice-to-smallest-value"])

    def test_runner_or_github_means_all(self):
        self.assertIsNone(rg.skills_from_paths(["evals/run_golden.py"], self.KNOWN))
        self.assertIsNone(rg.skills_from_paths([".github/workflows/ci.yml"], self.KNOWN))

    def test_unrelated_paths_mean_none(self):
        self.assertEqual(rg.skills_from_paths(["README.md", "tools/validate.py", "evals/test_run_golden.py"],
                                              self.KNOWN), [])

    def test_skill_without_cases_is_dropped(self):
        self.assertEqual(rg.skills_from_paths(["skills/new-skill/SKILL.md"], self.KNOWN), [])


class FakeRepo(unittest.TestCase):
    """A temp repo with skills/demo/scripts/*.py and evals/golden/demo/<case>/."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="golden-repo-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        write(self.repo / "skills/demo/scripts/report.py", """
            import json, os, shutil, sys, time
            data = {"cwd": os.getcwd(), "arg": sys.argv[1:], "duration_s": time.time() % 7,
                    "has_git": bool(shutil.which("git")), "has_ls": bool(shutil.which("ls")),
                    "files": sorted(os.listdir("."))}
            print(json.dumps(data))
            sys.exit(int(os.environ.get("DEMO_EXIT", "0")) or (1 if "--fail" in sys.argv else 0))
        """)
        write(self.repo / "skills/demo/scripts/text.py", """
            import os
            print("hello from " + os.getcwd())
        """)

    def case(self, name: str, spec: dict, files: dict[str, str] | None = None, skill: str = "demo") -> Path:
        d = self.repo / "evals/golden" / skill / name
        write(d / "case.json", json.dumps(spec))
        for rel, text in (files or {}).items():
            write(d / "fixture" / rel, text)
        return d


class RunCaseTest(FakeRepo):
    def test_update_then_pass_and_normalized(self):
        d = self.case("one", {"script": "report.py", "args": ["{fixture}/in.txt"], "expect_exit": 0},
                      {"in.txt": "x\n"})
        code, out = run_main(["--update"], self.repo)
        self.assertEqual(code, 0, out)
        expected = json.loads((d / "expected.json").read_text())
        self.assertEqual(expected["cwd"], "<TMP>")
        self.assertEqual(expected["arg"], ["<TMP>/in.txt"])
        self.assertEqual(expected["duration_s"], 0)
        self.assertEqual(expected["files"], ["in.txt"])
        self.assertTrue(expected["has_git"] or not HAS_GIT)
        self.assertFalse(expected["has_ls"], "PATH must hold only python3 and git")
        code, out = run_main([], self.repo)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS    demo/one", out)

    def test_fixture_is_not_modified(self):
        d = self.case("one", {"script": "report.py", "expect_exit": 0,
                              "setup": [["python3", "-c", "open('new.txt', 'w').write('x')"]]},
                      {"in.txt": "x\n"})
        run_main(["--update"], self.repo)
        self.assertEqual(sorted(p.name for p in (d / "fixture").iterdir()), ["in.txt"])
        self.assertIn("new.txt", json.loads((d / "expected.json").read_text())["files"])

    def test_mismatch_prints_diff_and_exits_1(self):
        d = self.case("one", {"script": "report.py", "expect_exit": 0})
        run_main(["--update"], self.repo)
        data = json.loads((d / "expected.json").read_text())
        data["arg"] = ["changed"]
        (d / "expected.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        code, out = run_main([], self.repo)
        self.assertEqual(code, 1)
        self.assertIn("FAIL    demo/one", out)
        self.assertIn('-  "arg": [', out)
        self.assertIn("+  \"arg\": [],", out)

    def test_exit_code_mismatch_fails_and_update_does_not_write(self):
        d = self.case("one", {"script": "report.py", "args": ["--fail"], "expect_exit": 0})
        code, out = run_main(["--update"], self.repo)
        self.assertEqual(code, 1)
        self.assertIn("exit 1, expected 0", out)
        self.assertFalse((d / "expected.json").exists())

    def test_missing_golden_fails(self):
        self.case("one", {"script": "report.py", "expect_exit": 0})
        code, out = run_main([], self.repo)
        self.assertEqual(code, 1)
        self.assertIn("missing expected.json", out)

    def test_text_output_uses_expected_txt(self):
        d = self.case("txt", {"script": "text.py", "expect_exit": 0})
        code, _ = run_main(["--update"], self.repo)
        self.assertEqual(code, 0)
        self.assertEqual((d / "expected.txt").read_text(), "hello from <TMP>\n")
        self.assertFalse((d / "expected.json").exists())
        self.assertEqual(run_main([], self.repo)[0], 0)

    def test_cwd_repo(self):
        d = self.case("r", {"script": "report.py", "cwd": "repo", "expect_exit": 0})
        run_main(["--update"], self.repo)
        self.assertEqual(json.loads((d / "expected.json").read_text())["cwd"], "<REPO>")

    def test_skill_filter_and_unknown_skill(self):
        self.case("one", {"script": "report.py", "expect_exit": 0})
        self.case("one", {"script": "report.py", "expect_exit": 0}, skill="other")
        code, out = run_main(["--update", "--skill", "demo", "--json"], self.repo)
        self.assertEqual(code, 0)
        self.assertEqual([c["case"] for c in json.loads(out)["cases"]], ["demo/one"])
        self.assertEqual(run_main(["--skill", "nope"], self.repo)[0], 2)

    def test_bad_case_json_is_usage_error(self):
        self.case("bad", {"args": []})
        self.assertEqual(run_main([], self.repo)[0], 2)

    def test_missing_script_is_reported(self):
        self.case("gone", {"script": "nope.py", "expect_exit": 0})
        code, out = run_main([], self.repo)
        self.assertEqual(code, 1)
        self.assertIn("script not found", out)

    def test_copy_step_cannot_escape(self):
        self.case("esc", {"script": "report.py", "expect_exit": 0,
                          "setup": [{"copy": "../../../../skills/demo/scripts/text.py", "to": "t.py"}]})
        code, out = run_main([], self.repo)
        self.assertEqual(code, 1)
        self.assertIn("copy step must stay inside", out)


@unittest.skipUnless(HAS_GIT, "git not installed")
class SetupStepsTest(FakeRepo):
    def test_git_setup_and_copy_step(self):
        d = self.case("g", {"script": "report.py", "expect_exit": 0, "setup": [
            ["git", "init", "-q", "-b", "main"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "base"],
            {"copy": "patches/extra.txt", "to": "sub/extra.txt"},
        ]}, {"a.txt": "a\n"})
        write(d / "patches/extra.txt", "extra\n")
        code, out = run_main(["--update"], self.repo)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((d / "expected.json").read_text())["files"], [".git", "a.txt", "sub"])

    def test_failing_setup_step_is_reported(self):
        self.case("g", {"script": "report.py", "expect_exit": 0, "setup": [["git", "commit", "-q", "-m", "x"]]})
        code, out = run_main([], self.repo)
        self.assertEqual(code, 1)
        self.assertIn("setup step", out)


@unittest.skipUnless(HAS_GIT, "git not installed")
class ChangedFromTest(FakeRepo):
    def setUp(self):
        super().setUp()
        for skill in ("demo", "other"):
            write(self.repo / f"skills/{skill}/scripts/report.py",
                  (self.repo / "skills/demo/scripts/report.py").read_text())
            self.case("one", {"script": "report.py", "expect_exit": 0}, skill=skill)
        write(self.repo / "evals/run_golden.py", "# stand-in\n")
        write(self.repo / "README.md", "readme\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        run_main(["--update"], self.repo)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "goldens")
        git(self.repo, "checkout", "-q", "-b", "topic")

    def commit(self, rel: str, text: str) -> None:
        write(self.repo / rel, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"change {rel}")

    def ran(self, argv: list[str]) -> tuple[int, list[str], dict]:
        code, out = run_main(argv + ["--json"], self.repo)
        data = json.loads(out)
        return code, [c["case"] for c in data["cases"]], data

    def test_no_skill_changed(self):
        self.commit("README.md", "changed\n")
        code, out = run_main(["--changed-from", "main"], self.repo)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no skills changed")

    def test_only_changed_skill_runs(self):
        self.commit("skills/other/SKILL.md", "x\n")
        code, cases, _ = self.ran(["--changed-from", "main"])
        self.assertEqual((code, cases), (0, ["other/one"]))

    def test_golden_change_counts(self):
        self.commit("evals/golden/demo/one/notes.md", "x\n")
        self.assertEqual(self.ran(["--changed-from", "main"])[1], ["demo/one"])

    def test_runner_change_runs_all(self):
        self.commit("evals/run_golden.py", "# changed\n")
        self.assertEqual(self.ran(["--changed-from", "main"])[1], ["demo/one", "other/one"])

    def test_changed_from_intersects_skill_filter(self):
        self.commit("skills/other/SKILL.md", "x\n")
        code, out = run_main(["--changed-from", "main", "--skill", "demo"], self.repo)
        self.assertEqual((code, out.strip()), (0, "no skills changed"))

    def test_bad_ref_is_usage_error(self):
        self.assertEqual(run_main(["--changed-from", "no-such-ref"], self.repo)[0], 2)


class RealGoldensTest(unittest.TestCase):
    def test_every_case_json_loads(self):
        cases = rg.discover(rg.REPO)
        self.assertTrue(cases)
        by_skill: dict[str, set[int]] = {}
        for c in cases:
            by_skill.setdefault(c.skill, set()).add(c.spec["expect_exit"])
            self.assertTrue((c.dir / "expected.json").is_file() or (c.dir / "expected.txt").is_file(), c.id)
        for skill, exits in by_skill.items():
            self.assertTrue((rg.REPO / "skills" / skill).is_dir(), skill)
            self.assertIn(0, exits, f"{skill} needs a passing case")
            self.assertTrue(exits - {0}, f"{skill} needs a failing case")


if __name__ == "__main__":
    unittest.main()
