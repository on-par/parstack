import contextlib
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
_spec = importlib.util.spec_from_file_location("parstack_check_issue", HERE.parent / "check_issue.py")
ci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci)


def call(args, runner=None, which=None):
    out = io.StringIO()
    kwargs = {}
    if runner:
        kwargs["runner"] = runner
    if which:
        kwargs["which"] = which
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = ci.main(args, **kwargs)
    return code, out.getvalue()


def md(text, title="T"):
    return ci.assess({"title": title, "body": text, "labels": []})


class VerdictTests(unittest.TestCase):
    def test_ready(self):
        code, out = call(["--file", str(FIXTURES / "ready.md"), "--json"])
        r = json.loads(out)
        self.assertEqual(r["verdict"], "ready", r)
        self.assertEqual(code, 0)
        self.assertEqual(r["title"], "Expire password reset links after one hour")
        self.assertEqual(r["detected"]["dependencies"], ["#41"])  # code block #99 ignored
        self.assertEqual(r["detected"]["criteria"][0]["proof"], "test_expired_token_rejected")
        self.assertEqual(r["detected"]["criteria"][1]["proof"], "`npm test -- reset`")
        self.assertIn("#41", r["notes"][0])

    def test_ready_with_assumptions(self):
        code, out = call(["--file", str(FIXTURES / "assumptions.md"), "--json"])
        r = json.loads(out)
        self.assertEqual(r["verdict"], "ready-with-assumptions")
        self.assertEqual(code, 0)
        text = "\n".join(r["assumptions"])
        self.assertIn("no proof for \"Results load fast", text)
        self.assertIn("vague wording (fast)", text)
        self.assertIn("no scope section", text)
        self.assertIn("no dependencies stated", text)
        code, _ = call(["--file", str(FIXTURES / "assumptions.md"), "--strict"])
        self.assertEqual(code, 1)

    def test_not_ready(self):
        code, out = call(["--file", str(FIXTURES / "not_ready.md"), "--json"])
        r = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(r["verdict"], "not-ready")
        text = "\n".join(r["missing"])
        self.assertIn("no acceptance criteria", text)
        self.assertIn("TBD", text)
        self.assertIn("open question: Which steps can be skipped?", text)

    def test_too_big(self):
        items = "\n".join(f"- [ ] thing {i} works (proof: test_thing_{i})" for i in range(6))
        r = md(f"## Acceptance criteria\n{items}\n## Scope\n- a\n## Out of scope\n- b\n"
               "## Dependencies\nNone\n")
        self.assertEqual(r["verdict"], "not-ready")
        self.assertIn("too big: 6 acceptance criteria", r["missing"][0])
        r = md(f"## Acceptance criteria\n{items}\n", title="T")
        self.assertEqual(ci.assess({"title": "T", "body": f"## Acceptance criteria\n{items}"},
                                   max_items=6)["missing"], [])

    def test_no_proof_anywhere_blocks(self):
        r = md("## Acceptance criteria\n- user sees a banner\n- banner closes\n")
        self.assertEqual(r["verdict"], "not-ready")
        self.assertIn("no proof named", r["missing"][0])

    def test_proof_section_downgrades_to_assumption(self):
        r = md("## Acceptance criteria\n- user sees a banner\n## Testing\n- `npm test -- banner`\n")
        self.assertNotIn("no proof named", " ".join(r["missing"]))
        self.assertIn("Testing/Verification section", " ".join(r["assumptions"]))

    def test_manual_proof_is_not_proof(self):
        r = md("## Acceptance criteria\n- user sees a banner (proof: manual)\n")
        self.assertIsNone(r["detected"]["criteria"][0]["proof"])

    def test_blocking_label(self):
        code, out = call(["--json-file", str(FIXTURES / "jira.json"), "--json"])
        r = json.loads(out)
        self.assertEqual(r["title"], "Rotate API keys")
        self.assertEqual(r["detected"]["criteria"][0]["proof"], "test_old_key_rejected")
        self.assertIn('label "blocked"', " ".join(r["missing"]))
        self.assertEqual(code, 1)


class ParsingTests(unittest.TestCase):
    def test_linear_export_label_style_headings(self):
        code, out = call(["--json-file", str(FIXTURES / "linear.json"), "--json"])
        r = json.loads(out)
        self.assertEqual(r["verdict"], "ready", r)
        self.assertTrue(r["detected"]["dependencies_none"])
        self.assertEqual(r["labels"], ["feature"])

    def test_checkbox_fallback_without_heading(self):
        r = md("Do the thing.\n\n- [ ] badge shows count (test: test_badge_count)\n")
        self.assertEqual(r["detected"]["criteria_source"], "checkboxes")
        self.assertEqual(len(r["detected"]["criteria"]), 1)

    def test_dependency_phrases(self):
        r = md("Blocked by #3 and #4. Depends on: org/repo#9, ENG-2. Requires #5.\n"
               "## Acceptance criteria\n- x (proof: test_x)\n")
        self.assertEqual(r["detected"]["dependencies"], ["#3", "#4", "org/repo#9", "ENG-2", "#5"])

    def test_nested_items_are_children(self):
        items = ci.parse_items(["- a", "  - proof: test_a", "- b", "    - note"])
        self.assertEqual([i["text"] for i in items], ["a", "b"])
        self.assertEqual(items[0]["children"], ["proof: test_a"])

    def test_lowercase_todo_is_not_unresolved(self):
        r = md("Build a todo list.\n## Acceptance criteria\n- add item (proof: test_add)\n")
        self.assertEqual(r["detected"]["unresolved"], [])


class GithubTests(unittest.TestCase):
    def test_gh_missing_is_skipped(self):
        code, out = call(["--gh", "5", "--json"], which=lambda _: None)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), {"skipped": "gh not found"})

    def test_gh_runner(self):
        payload = {"number": 5, "title": "Ready one", "url": "https://example.test/5",
                   "labels": [{"name": "bug"}],
                   "body": (FIXTURES / "ready.md").read_text().split("\n", 1)[1]}
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

        code, out = call(["--gh", "5", "--repo", "o/r", "--json"], runner=runner,
                         which=lambda _: "/usr/bin/gh")
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][:4], ["gh", "issue", "view", "5"])
        self.assertIn("--repo", calls[0])
        r = json.loads(out)
        self.assertEqual(r["verdict"], "ready")
        self.assertEqual(r["labels"], ["bug"])

    def test_gh_failure_is_exit_2(self):
        def runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")

        code, _ = call(["--gh", "5"], runner=runner, which=lambda _: "/usr/bin/gh")
        self.assertEqual(code, 2)


class InputTests(unittest.TestCase):
    def test_missing_file(self):
        code, _ = call(["--file", str(FIXTURES / "nope.md")])
        self.assertEqual(code, 2)

    def test_list_of_many_is_error(self):
        with self.assertRaises(ci.InputError):
            ci.from_exported([{}, {}])


if __name__ == "__main__":
    unittest.main()
