import contextlib
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
_spec = importlib.util.spec_from_file_location("parstack_order_queue", HERE.parent / "order_queue.py")
oq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oq)

NOW = "2026-11-17T00:00:00Z"


def call(args, runner=None, which=None):
    out = io.StringIO()
    kwargs = {}
    if runner:
        kwargs["runner"] = runner
    if which:
        kwargs["which"] = which
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = oq.main(args, **kwargs)
    return code, out.getvalue()


def numbers(layers):
    return [[x["number"] for x in layer] for layer in layers]


def issue(n, body="", labels=(), state="open", updated=None):
    return {"number": n, "title": f"Issue {n}", "body": body, "labels": list(labels),
            "updatedAt": updated, "state": state}


class FixtureTests(unittest.TestCase):
    def setUp(self):
        code, out = call(["--file", str(FIXTURES / "queue.json"), "--now", NOW,
                          "--priority-label", "p0,p1", "--json"])
        self.code, self.r = code, json.loads(out)

    def test_layers_with_priority(self):
        self.assertEqual(numbers(self.r["layers"]), [[1, 4, 9, 11], [3, 2]])
        self.assertEqual(self.r["order"], [1, 4, 9, 11, 3, 2])

    def test_blocked_reasons(self):
        blocked = {b["number"]: b["reasons"] for b in self.r["blocked"]}
        self.assertEqual(sorted(blocked), [5, 6, 7, 8, 10])
        self.assertIn("depends on unknown #99", blocked[5])
        self.assertIn("depends on blocked #5", blocked[6])
        self.assertTrue(any("cycle" in r for r in blocked[7]))
        self.assertIn('label "blocked"', blocked[10])

    def test_cycles_missing_stale(self):
        self.assertEqual(self.r["cycles"], [[7, 8]])
        statuses = {(m["issue"], m["dependency"]): m["status"] for m in self.r["missing"]}
        self.assertEqual(statuses, {(5, "#99"): "unknown", (9, "#50"): "closed",
                                    (11, "other/repo#3"): "external"})
        self.assertEqual(self.r["stale"], [{"issue": 4, "days": 108}])

    def test_exit_1_on_cycle_or_unknown(self):
        self.assertEqual(self.code, 1)

    def test_human_output(self):
        _, out = call(["--file", str(FIXTURES / "queue.json"), "--now", NOW])
        self.assertIn("1. #1 Auth basics, #4 Report export", out)
        self.assertIn("cycle: #7 -> #8 -> #7", out)
        self.assertIn("stale (no update in >30d): #4 (108d)", out)


class MarkdownTests(unittest.TestCase):
    def test_markdown_list(self):
        code, out = call(["--file", str(FIXTURES / "queue.md"), "--now", NOW,
                          "--priority-label", "p0", "--priority-label", "p1", "--json"])
        r = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(numbers(r["layers"]), [[20, 23], [21], [22]])
        self.assertEqual(r["layers"][1][0]["title"], "Search results page")
        self.assertEqual(r["layers"][0][0]["title"], "Add search box")
        self.assertEqual(r["missing"], [{"issue": 23, "dependency": "#19", "status": "closed"}])

    def test_no_items_is_input_error(self):
        with self.assertRaises(oq.InputError):
            oq.from_markdown("just text\n")


class AnalyzeTests(unittest.TestCase):
    def test_multiple_refs_in_one_phrase(self):
        deps, ext = oq.parse_deps(issue(1, "Depends on #2, #3 and #4; after #5"))
        self.assertEqual(deps, [2, 3, 4])
        self.assertEqual(ext, [])

    def test_label_dependencies(self):
        deps, _ = oq.parse_deps(issue(1, labels=["blocked-by:7", "after #8", "p1"]))
        self.assertEqual(deps, [7, 8])

    def test_self_dependency_is_cycle(self):
        r = oq.analyze([issue(1, "depends on #1")])
        self.assertEqual(r["cycles"], [[1]])
        self.assertEqual(r["order"], [])

    def test_custom_blocked_labels(self):
        r = oq.analyze([issue(1, labels=["on-hold"]), issue(2, labels=["blocked"])],
                       blocked_labels=["on-hold"])
        self.assertEqual(r["order"], [2])

    def test_lookup_resolves_outside_deps(self):
        states = {40: "closed", 41: "open"}
        r = oq.analyze([issue(1, "depends on #40"), issue(2, "depends on #41"),
                        issue(3, "depends on #42")], lookup=states.get)
        self.assertEqual(r["order"], [1])
        statuses = [m["status"] for m in r["missing"]]
        self.assertEqual(statuses, ["closed", "open-not-listed", "unknown"])


class DependencyRuleTests(unittest.TestCase):
    """Only explicit verbs or bullets in a Dependencies section make an edge."""

    def test_shared_cases(self):
        cases = json.loads((FIXTURES / "dependency_cases.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(case["name"]):
                local, external = oq.parse_deps(issue(1, case["body"]))
                self.assertEqual([f"#{n}" for n in local] + external, case["refs"])

    def test_prose_refs_do_not_order_the_queue(self):
        r = oq.analyze([
            issue(12, "Builds on merged #40."),
            issue(33, "Related: #40, #41"),
            issue(40, "The reset email test fails after #33 lands."),
            issue(41, "## Related\n- #12\n\nDependencies: None."),
        ])
        self.assertEqual(numbers(r["layers"]), [[12, 33, 40, 41]])
        self.assertEqual(r["missing"], [])

    def test_explicit_verbs_still_order_the_queue(self):
        r = oq.analyze([issue(11), issue(12, "Depends on #11"), issue(13, "Blocked by #12"),
                        issue(14, "Requires #13")])
        self.assertEqual(numbers(r["layers"]), [[11], [12], [13], [14]])

    def test_markdown_after_is_not_a_dependency(self):
        issues = oq.from_markdown("- #20 Search box\n- #21 Results page (after #20)\n")
        self.assertEqual(numbers(oq.analyze(issues)["layers"]), [[20, 21]])


class GithubTests(unittest.TestCase):
    def test_gh_missing_is_skipped(self):
        code, out = call(["--gh", "--json"], which=lambda _: None)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), {"skipped": "gh not found"})

    def test_gh_list_and_lookup(self):
        listing = [{"number": 1, "title": "A", "body": "depends on #9", "labels": [],
                    "updatedAt": "2026-11-16T00:00:00Z", "state": "OPEN"},
                   {"number": 2, "title": "B", "body": "Requires #1", "labels": [],
                    "updatedAt": "2026-11-16T00:00:00Z", "state": "OPEN"}]
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            if cmd[:3] == ["gh", "issue", "list"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(listing), "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"number": 9, "state": "CLOSED"}), "")

        code, out = call(["--gh", "--repo", "o/r", "--limit", "5", "--now", NOW, "--json"],
                         runner=runner, which=lambda _: "/usr/bin/gh")
        r = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(numbers(r["layers"]), [[1], [2]])
        self.assertIn("--limit", calls[0])
        self.assertEqual(calls[1][:4], ["gh", "issue", "view", "9"])

    def test_gh_no_resolve(self):
        listing = [{"number": 1, "title": "A", "body": "depends on #9", "labels": [], "state": "OPEN"}]

        def runner(cmd, **kw):
            assert cmd[:3] == ["gh", "issue", "list"], cmd
            return subprocess.CompletedProcess(cmd, 0, json.dumps(listing), "")

        code, _ = call(["--gh", "--no-resolve"], runner=runner, which=lambda _: "/usr/bin/gh")
        self.assertEqual(code, 1)


class InputTests(unittest.TestCase):
    def test_missing_file(self):
        self.assertEqual(call(["--file", str(FIXTURES / "nope.json")])[0], 2)

    def test_bad_now(self):
        self.assertEqual(call(["--file", str(FIXTURES / "queue.md"), "--now", "yesterday"])[0], 2)

    def test_issues_wrapper_object(self):
        issues = oq.from_json_text('{"issues": [{"number": "#3", "title": "x"}]}')
        self.assertEqual(issues[0]["number"], 3)


if __name__ == "__main__":
    unittest.main()
