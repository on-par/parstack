import contextlib
import importlib.util
import io
import json
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
        self.assertIn("covers: AC2 (Reset links expire after one hour)", body)
        self.assertIn("- Depends on S1", body)
        self.assertIn("## Out of scope", body)

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


if __name__ == "__main__":
    unittest.main()
