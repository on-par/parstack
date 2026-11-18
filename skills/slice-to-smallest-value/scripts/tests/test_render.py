import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
FIXTURES = HERE / "fixtures"


def load(name):
    spec = importlib.util.spec_from_file_location(f"parstack_slice_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


render = load("render")


def call(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = render.main(args)
    return code, out.getvalue()


class RenderTests(unittest.TestCase):
    def test_json_bodies_in_dependency_order(self):
        code, out = call([str(FIXTURES / "good.json"), "--json"])
        self.assertEqual(code, 0)
        items = json.loads(out)
        self.assertEqual([i["id"] for i in items], ["S1", "S2", "S3"])
        body = items[1]["body"]
        self.assertIn("## Acceptance criteria", body)
        self.assertIn("- [ ] A 61-minute-old token is rejected", body)
        self.assertIn("proof: test_expired_token_rejected", body)
        self.assertIn("- Depends on S1", body)
        self.assertIn("## Out of scope\n- Configurable expiry time\n", body)

    def test_issue_map_rewrites_dependencies(self):
        code, out = call([str(FIXTURES / "good.json"), "--json", "--issue-map", "S1=41, S2=#42"])
        self.assertEqual(code, 0)
        self.assertIn("- Depends on #41", json.loads(out)[2]["body"])

    def test_no_dependencies_says_none(self):
        _, out = call([str(FIXTURES / "good.json"), "--json"])
        self.assertIn("## Dependencies\n- None", json.loads(out)[0]["body"])

    def test_markdown_output(self):
        code, out = call([str(FIXTURES / "slices.md")])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# S1: Download current page of orders as CSV"))

    def test_bad_issue_map_is_exit_2(self):
        code, _ = call([str(FIXTURES / "good.json"), "--issue-map", "S1"])
        self.assertEqual(code, 2)


def bodies(doc, extra=()):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(doc, fh)
        path = fh.name
    try:
        code, out = call([path, "--json", *extra])
    finally:
        Path(path).unlink()
    return code, {i["id"]: i["body"] for i in json.loads(out)} if code == 0 else {}


def good():
    return json.loads((FIXTURES / "good.json").read_text())


class SchemaFieldTests(unittest.TestCase):
    def test_context_and_out_of_scope_rendered(self):
        _, b = bodies(good())
        self.assertIn("## Context\nReset tokens live in the existing sessions table.\n", b["S1"])
        self.assertIn("## Out of scope\n- Link expiry\n- Rate limiting\n", b["S1"])
        self.assertNotIn("## Context", b["S2"])

    def test_empty_out_of_scope_is_not_invented(self):
        doc = good()
        doc["slices"][2]["out_of_scope"] = []
        del doc["slices"][1]["out_of_scope"]
        _, b = bodies(doc)
        for sid in ("S2", "S3"):
            self.assertNotIn("Out of scope", b[sid])
            self.assertNotIn("Anything not listed", b[sid])

    def test_part_of_only_with_parent_issue(self):
        _, b = bodies(good())
        self.assertNotIn("Part of", b["S1"])
        self.assertNotIn("Users can reset a forgotten password", b["S1"])
        code, b = bodies(good(), ["--parent-issue", "12"])
        self.assertEqual(code, 0)
        self.assertIn("\nPart of #12\n", b["S1"])
        code, _ = bodies(good(), ["--parent-issue", "P1"])
        self.assertEqual(code, 2)

    def test_covers_only_with_show_covers(self):
        _, b = bodies(good())
        self.assertNotIn("covers:", b["S2"])
        _, b = bodies(good(), ["--show-covers"])
        self.assertIn("  - covers: AC2 (Reset links expire after one hour)", b["S2"])

    def test_external_dependencies_rendered_as_is(self):
        doc = good()
        doc["slices"][1]["depends_on"] = ["S1", "#40", "acme/api#7", "PROJ-12"]
        _, b = bodies(doc, ["--issue-map", "S1=41"])
        self.assertIn("## Dependencies\n- Depends on #41\n- Depends on #40\n"
                      "- Depends on acme/api#7\n- Depends on PROJ-12\n", b["S2"])

    def test_dropped_ideas_get_no_issue(self):
        doc = good()
        doc["parent"]["dropped"] = [{"text": "Reset by SMS", "reason": "no SMS provider",
                                     "covered_by": "#40"}]
        _, b = bodies(doc)
        self.assertEqual(sorted(b), ["S1", "S2", "S3"])
        self.assertFalse(any("Reset by SMS" in body for body in b.values()))


if __name__ == "__main__":
    unittest.main()
