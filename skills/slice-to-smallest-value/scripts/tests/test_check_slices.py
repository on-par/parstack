import contextlib
import copy
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


cs = load("check_slices")


def good():
    return json.loads((FIXTURES / "good.json").read_text())


def run(doc, overlap="fail"):
    result = cs.check(copy.deepcopy(doc))
    cs.apply_overlap_policy(result, overlap)
    return result


def codes(result, kind="errors"):
    return [e["code"] for e in result[kind]]


def call_main(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cs.main(args)
    return code, out.getvalue()


class GoodDocTests(unittest.TestCase):
    def test_good_fixture_passes(self):
        r = run(good())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["warnings"], [])
        self.assertEqual(r["layers"], [["S1"], ["S2", "S3"]])
        self.assertEqual(r["order"], ["S1", "S2", "S3"])

    def test_markdown_fenced_block(self):
        doc = cs.load_doc(str(FIXTURES / "slices.md"))
        self.assertTrue(run(doc)["ok"])

    def test_main_exit_codes_and_json(self):
        code, out = call_main([str(FIXTURES / "good.json"), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        code, out = call_main([str(FIXTURES / "good.json")])
        self.assertIn("PASS", out)
        self.assertIn("2. S2, S3", out)


class SizeTests(unittest.TestCase):
    def test_too_much_scope(self):
        doc = good()
        doc["slices"][0]["scope"] = [f"item {i}" for i in range(6)]
        self.assertIn("too-much-scope", codes(run(doc)))

    def test_too_many_criteria(self):
        doc = good()
        base = doc["slices"][1]["criteria"][0]
        doc["slices"][1]["criteria"] = [dict(base, id=f"S2.{i}") for i in range(6)]
        self.assertIn("too-many-criteria", codes(run(doc)))

    def test_five_is_allowed(self):
        doc = good()
        doc["slices"][0]["scope"] = [f"item {i}" for i in range(5)]
        self.assertTrue(run(doc)["ok"])

    def test_no_criteria_and_no_value(self):
        doc = good()
        doc["slices"][2]["criteria"] = []
        doc["slices"][2]["value"] = ""
        c = codes(run(doc))
        self.assertIn("no-criteria", c)
        self.assertIn("no-value", c)


class ProofTests(unittest.TestCase):
    def test_rejects_vague_proofs(self):
        for proof in ["", "manual", "Manually click around", "works", "tests", "TBD",
                      "user can log in", "test: it is fine", None]:
            with self.subTest(proof=proof):
                self.assertIsNotNone(cs.proof_problem(proof))

    def test_accepts_concrete_proofs(self):
        for proof in ["test_expired_token_rejected", "tests/auth/reset.test.ts",
                      "OrdersExportTests.DownloadsVisibleRows", "`make verify`",
                      "npm test -- reset", "test: 'rejects expired token'",
                      "pkg/auth::TestExpiry", "dotnet test --filter Reset"]:
            with self.subTest(proof=proof):
                self.assertIsNone(cs.proof_problem(proof))

    def test_weak_proof_fails_check(self):
        doc = good()
        doc["slices"][0]["criteria"][0]["proof"] = "manual QA"
        r = run(doc)
        self.assertIn("weak-proof", codes(r))
        self.assertFalse(r["ok"])


class CoverageTests(unittest.TestCase):
    def test_uncovered_parent_criterion(self):
        doc = good()
        doc["slices"][2]["criteria"][0]["covers"] = []
        r = run(doc)
        self.assertIn("uncovered", codes(r))
        self.assertIn("covers-nothing", codes(r, "warnings"))

    def test_unknown_parent_criterion(self):
        doc = good()
        doc["slices"][0]["criteria"][0]["covers"].append("AC9")
        self.assertIn("unknown-parent-criterion", codes(run(doc)))

    def test_no_parent_criteria_warns(self):
        doc = good()
        doc["parent"]["criteria"] = []
        for s in doc["slices"]:
            for c in s["criteria"]:
                c["covers"] = []
        r = run(doc)
        self.assertTrue(r["ok"], r["errors"])
        self.assertIn("no-parent-criteria", codes(r, "warnings"))


class GraphTests(unittest.TestCase):
    def test_unknown_dependency(self):
        doc = good()
        doc["slices"][1]["depends_on"] = ["S9"]
        self.assertIn("unknown-dependency", codes(run(doc)))

    def test_self_dependency(self):
        doc = good()
        doc["slices"][0]["depends_on"] = ["S1"]
        self.assertIn("self-dependency", codes(run(doc)))

    def test_cycle_detected_and_left_out_of_order(self):
        doc = good()
        doc["slices"][0]["depends_on"] = ["S2"]
        r = run(doc)
        self.assertIn("cycle", codes(r))
        self.assertEqual(r["cycles"], [["S1", "S2"]])
        self.assertNotIn("S1", r["order"])

    def test_duplicate_slice_id(self):
        doc = good()
        doc["slices"][2]["id"] = "S2"
        self.assertIn("duplicate-id", codes(run(doc)))


class OverlapTests(unittest.TestCase):
    def test_independent_slices_sharing_a_file_fail(self):
        doc = good()
        doc["slices"][2]["files"].append("src/auth/reset.ts")
        r = run(doc)
        self.assertIn("file-overlap", codes(r))
        self.assertEqual(r["overlaps"], [{"slices": ["S2", "S3"], "files": ["src/auth/reset.ts"]}])

    def test_overlap_warn_policy(self):
        doc = good()
        doc["slices"][2]["files"].append("src/auth/reset.ts")
        r = run(doc, overlap="warn")
        self.assertTrue(r["ok"])
        self.assertIn("file-overlap", codes(r, "warnings"))

    def test_dependent_slices_may_share_files(self):
        doc = good()  # S1 and S2 both touch src/auth/reset.ts; S2 depends on S1
        self.assertEqual(run(doc)["overlaps"], [])

    def test_dir_and_glob_overlap(self):
        self.assertTrue(cs.files_overlap("src/auth/", "src/auth/x.ts"))
        self.assertTrue(cs.files_overlap("src/**/*.ts", "src/auth/x.ts"))
        self.assertTrue(cs.files_overlap("./a.ts", "a.ts"))
        self.assertFalse(cs.files_overlap("src/auth.ts", "src/auth/x.ts"))


class HeuristicTests(unittest.TestCase):
    def test_horizontal_title_warns(self):
        doc = good()
        self.assertEqual(run(doc)["warnings"], [])
        for title in ["Database schema", "Backend only", "Set up the API layer", "UI"]:
            with self.subTest(title=title):
                doc["slices"][2]["title"] = title
                self.assertIn("horizontal", codes(run(doc), "warnings"))
        doc["slices"][2]["title"] = "Backend only"
        code, _ = call_main_doc(doc, ["--strict"])
        self.assertEqual(code, 1)


def call_main_doc(doc, extra):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(doc, fh)
        path = fh.name
    try:
        return call_main([path] + extra)
    finally:
        Path(path).unlink()


class InputTests(unittest.TestCase):
    def test_missing_file_is_exit_2(self):
        code, _ = call_main([str(FIXTURES / "nope.json")])
        self.assertEqual(code, 2)

    def test_missing_slices_is_exit_2(self):
        code, out = call_main_doc({"parent": {}}, ["--json"])
        self.assertEqual(code, 2)
        self.assertIn("slices", json.loads(out)["input_error"])

    def test_extract_prefers_block_with_slices(self):
        text = '```json\n{"a": 1}\n```\n\n```json\n{"slices": []}\n```\n'
        self.assertEqual(json.loads(cs.extract_json_text(text)), {"slices": []})


if __name__ == "__main__":
    unittest.main()
