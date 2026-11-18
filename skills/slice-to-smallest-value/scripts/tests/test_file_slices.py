import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
FIXTURES = HERE / "fixtures"
REPO = "acme/notes-app"


def load(name):
    spec = importlib.util.spec_from_file_location(f"parstack_slice_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fs = load("file_slices")


def good():
    return json.loads((FIXTURES / "good.json").read_text())


class Workspace:
    """A temp dir with a slices file and a fake `gh` that records calls and never touches the network."""

    def __init__(self, doc):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.slices = self.dir / "slices.json"
        self.slices.write_text(json.dumps(doc))
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        self.gh = bin_dir / "gh"
        self.gh.write_text(f"#!{sys.executable}\n" + (FIXTURES / "fake_gh.py").read_text())
        self.gh.chmod(0o755)
        self.map = self.dir / "slices.issues.json"

    def run(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = [str(self.slices), "--repo", REPO, "--gh-bin", str(self.gh), *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fs.main(argv)
        return code, out.getvalue(), err.getvalue()

    def state(self):
        path = self.gh.parent / "state.json"
        return json.loads(path.read_text()) if path.exists() else {"issues": [], "calls": []}

    def creates(self):
        return [c for c in self.state()["calls"] if c[:2] == ["issue", "create"]]

    def fail_title(self, title):
        (self.gh.parent / "fail_titles").write_text(title + "\n")

    def close(self):
        self._tmp.cleanup()


class FileSlicesCase(unittest.TestCase):
    def workspace(self, doc=None):
        ws = Workspace(doc or good())
        self.addCleanup(ws.close)
        return ws


class DryRunTests(FileSlicesCase):
    def test_prints_plan_in_order_and_posts_nothing(self):
        ws = self.workspace()
        code, out, _ = ws.run("--label", "enhancement", "--label", "area: auth")
        self.assertEqual(code, 0, out)
        self.assertIn("dry run: 3 issues for acme/notes-app", out)
        order = [out.index(f"title: {t}") for t in ("Request a link and set a new password",
                                                     "Expire reset links after one hour",
                                                     "Rate-limit reset requests")]
        self.assertEqual(order, sorted(order))
        self.assertIn("labels: enhancement, area: auth", out)
        self.assertIn("- Depends on S1 (not created yet)", out)
        self.assertIn("readiness: ready\n", out)
        self.assertEqual(ws.state()["calls"], [])
        self.assertFalse(ws.map.exists())

    def test_needs_no_gh_and_has_no_default_labels(self):
        ws = self.workspace()
        code, out, _ = ws.run("--gh-bin", str(ws.dir / "missing-gh"))
        self.assertEqual(code, 0)
        self.assertIn("labels: (none)", out)

    def test_body_never_repeats_title_as_heading(self):
        ws = self.workspace()
        _, out, _ = ws.run("--json")
        for issue in json.loads(out)["issues"]:
            self.assertTrue(issue["body"].startswith("## Value\n"), issue["body"])
            self.assertNotIn(f"# {issue['title']}", issue["body"])

    def test_not_ready_slice_fails_dry_run(self):
        doc = good()
        doc["slices"][2]["context"] = "Retry policy is TBD."
        ws = self.workspace(doc)
        code, out, _ = ws.run()
        self.assertEqual(code, 1)
        self.assertIn("missing: unresolved markers: TBD", out)
        self.assertIn("would refuse: S3 not ready", out)

    def test_slice_check_errors_refuse(self):
        doc = good()
        doc["slices"][1]["depends_on"] = ["S9"]
        ws = self.workspace(doc)
        code, out, _ = ws.run("--create")
        self.assertEqual(code, 1)
        self.assertIn("depends on unknown slice S9", out)
        self.assertIn("refusing to file", out)
        self.assertEqual(ws.state()["calls"], [])


class CreateTests(FileSlicesCase):
    def test_creates_in_dependency_order_and_rewrites_dependencies(self):
        ws = self.workspace()
        code, out, err = ws.run("--create", "--label", "enhancement")
        self.assertEqual(code, 0, out + err)
        issues = ws.state()["issues"]
        self.assertEqual([(i["number"], i["title"]) for i in issues], [
            (41, "Request a link and set a new password"),
            (42, "Expire reset links after one hour"),
            (43, "Rate-limit reset requests"),
        ])
        self.assertIn("## Dependencies\n- Depends on #41\n", issues[1]["body"])
        self.assertIn("## Dependencies\n- Depends on #41\n", issues[2]["body"])
        for i in issues:
            self.assertEqual(i["labels"], ["enhancement"])
            self.assertEqual(i["repo"], REPO)
            self.assertTrue(i["body"].startswith("## Value\n"))
            self.assertNotIn("not created yet", i["body"])
        # each create is followed by a readiness re-check of the new issue
        calls = [c[:3] for c in ws.state()["calls"]]
        self.assertEqual(calls, [["issue", "create", "--repo"], ["issue", "view", "41"],
                                 ["issue", "create", "--repo"], ["issue", "view", "42"],
                                 ["issue", "create", "--repo"], ["issue", "view", "43"]])
        self.assertIn("re-check #42: ready", out)
        saved = json.loads(ws.map.read_text())
        self.assertEqual(saved["repo"], REPO)
        self.assertEqual({k: v["number"] for k, v in saved["issues"].items()},
                         {"S1": 41, "S2": 42, "S3": 43})
        self.assertEqual(saved["issues"]["S1"]["url"], "https://github.com/acme/notes-app/issues/41")

    def test_external_dependencies_pass_through(self):
        doc = good()
        doc["slices"][1]["depends_on"] = ["S1", "#40", "acme/api#7", "PROJ-12"]
        ws = self.workspace(doc)
        code, out, _ = ws.run("--create")
        self.assertEqual(code, 0, out)
        self.assertIn("## Dependencies\n- Depends on #41\n- Depends on #40\n"
                      "- Depends on acme/api#7\n- Depends on PROJ-12\n", ws.state()["issues"][1]["body"])

    def test_refuses_not_ready_issue_before_posting_anything(self):
        doc = good()
        doc["slices"][2]["context"] = "Retry policy is TBD."
        ws = self.workspace(doc)
        code, out, _ = ws.run("--create")
        self.assertEqual(code, 1)
        self.assertIn('refused: S3 "Rate-limit reset requests" is not-ready', out)
        self.assertIn("missing: unresolved markers: TBD", out)
        self.assertIn("already created: none", out)
        self.assertEqual(ws.creates(), [])

    def test_refusal_lists_already_created_numbers(self):
        doc = good()
        doc["slices"][1]["context"] = "Open question: TBD."
        ws = self.workspace(doc)
        ws.map.write_text(json.dumps({"repo": REPO, "issues": {
            "S1": {"number": 12, "url": "https://github.com/acme/notes-app/issues/12"}}}))
        code, out, _ = ws.run("--create", "--resume")
        self.assertEqual(code, 1)
        self.assertIn("refused: S2", out)
        self.assertIn("already created:\n  S1 -> #12 https://github.com/acme/notes-app/issues/12", out)
        self.assertEqual(ws.creates(), [])

    def test_gh_missing_is_skipped_exit_2(self):
        ws = self.workspace()
        code, _, err = ws.run("--create", "--gh-bin", str(ws.dir / "missing-gh"))
        self.assertEqual(code, 2)
        self.assertIn("skipped: gh not found", err)

    def test_bad_repo_is_exit_2(self):
        ws = self.workspace()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fs.main([str(ws.slices), "--repo", "notes-app"])
        self.assertEqual(code, 2)
        self.assertIn("want owner/name", err.getvalue())


class ResumeTests(FileSlicesCase):
    def test_partial_run_resumes_without_duplicates(self):
        ws = self.workspace()
        ws.fail_title("Expire reset links after one hour")
        code, out, _ = ws.run("--create")
        self.assertEqual(code, 1)
        self.assertIn("failed: gh issue create for S2", out)
        self.assertIn("already created:\n  S1 -> #41", out)
        self.assertEqual(list(json.loads(ws.map.read_text())["issues"]), ["S1"])

        (ws.gh.parent / "fail_titles").unlink()
        code, _, err = ws.run("--create")  # without --resume the existing map blocks a duplicate run
        self.assertEqual(code, 2)
        self.assertIn("--resume", err)

        code, out, _ = ws.run("--create", "--resume")
        self.assertEqual(code, 0, out)
        self.assertIn("skip S1: already #41", out)
        titles = [i["title"] for i in ws.state()["issues"]]
        self.assertEqual(titles.count("Request a link and set a new password"), 1)
        self.assertIn("- Depends on #41\n", ws.state()["issues"][1]["body"])
        saved = json.loads(ws.map.read_text())["issues"]
        self.assertEqual({k: v["number"] for k, v in saved.items()}, {"S1": 41, "S2": 42, "S3": 43})

        before = len(ws.creates())
        code, out, _ = ws.run("--create", "--resume")
        self.assertEqual(code, 0)
        self.assertEqual(len(ws.creates()), before)
        self.assertIn("done: 0 created, 3 skipped", out)

    def test_dry_run_resume_shows_known_numbers(self):
        ws = self.workspace()
        ws.map.write_text(json.dumps({"repo": REPO, "issues": {"S1": {"number": 41}}}))
        code, out, _ = ws.run("--resume")
        self.assertEqual(code, 0)
        self.assertIn("already #41 (from slices.issues.json); skipped", out)
        self.assertIn("- Depends on #41\n", out)
        self.assertNotIn("not created yet", out)

    def test_map_for_another_repo_is_rejected(self):
        ws = self.workspace()
        ws.map.write_text(json.dumps({"repo": "acme/other", "issues": {"S1": {"number": 41}}}))
        code, _, err = ws.run("--create", "--resume")
        self.assertEqual(code, 2)
        self.assertIn("acme/other", err)
        self.assertEqual(ws.creates(), [])

    def test_map_out_overrides_default_path(self):
        ws = self.workspace()
        target = ws.dir / "out" / "map.json"
        target.parent.mkdir()
        code, _, _ = ws.run("--create", "--map-out", str(target))
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())
        self.assertFalse(ws.map.exists())

    def test_missing_map_directory_fails_before_creating(self):
        ws = self.workspace()
        code, _, err = ws.run("--create", "--map-out", str(ws.dir / "nope" / "map.json"))
        self.assertEqual(code, 2)
        self.assertIn("map directory not found", err)
        self.assertEqual(ws.creates(), [])


if __name__ == "__main__":
    unittest.main()
