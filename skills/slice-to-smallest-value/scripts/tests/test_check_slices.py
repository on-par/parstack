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

    def test_doc_paths_and_pr_prose_are_weak(self):
        for proof in ["see docs/guide.md", "PR body lists timings", "`docs/guide.md`",
                      "src/auth/reset.ts", "README.md"]:
            with self.subTest(proof=proof):
                self.assertIsNotNone(cs.proof_problem(proof))
        self.assertIn("not a test", cs.proof_problem("see docs/guide.md"))

    def test_test_files_names_and_commands_pass(self):
        for proof in ["tests/reset.test.ts::expires", "test_expired_token",
                      "`time npm run test -- reset`", "`pytest`", "e2e/reset.spec.ts",
                      "pkg/reset/reset_test.go", "test/test_reset.py", "reset_spec.rb"]:
            with self.subTest(proof=proof):
                self.assertIsNone(cs.proof_problem(proof))

    def test_pr_prose_criterion_warns(self):
        doc = good()
        doc["slices"][2]["criteria"][0]["text"] = "PR body records request timings before and after"
        r = run(doc)
        self.assertTrue(r["ok"], r["errors"])
        self.assertIn("prose-criterion", codes(r, "warnings"))

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

    def test_external_dependencies_accepted(self):
        doc = good()
        doc["slices"][1]["depends_on"] = ["S1", "#40", "acme/api#7", "PROJ-12"]
        r = run(doc)
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["external_dependencies"], {"S2": ["#40", "acme/api#7", "PROJ-12"]})
        self.assertEqual(r["layers"], [["S1"], ["S2", "S3"]])
        self.assertEqual(r["cycles"], [])

    def test_external_dependencies_skip_cycle_and_overlap(self):
        doc = good()
        # S3 shares a file with S2; an external dep on both must not count as a link
        doc["slices"][2]["files"].append("src/auth/reset.ts")
        doc["slices"][1]["depends_on"] = ["S1", "#40"]
        doc["slices"][2]["depends_on"] = ["S1", "#40"]
        r = run(doc)
        self.assertEqual(r["cycles"], [])
        self.assertIn("file-overlap", codes(r))

    def test_external_dependencies_in_run_order_output(self):
        doc = good()
        doc["slices"][1]["depends_on"] = ["S1", "#40", "acme/api#7"]
        _, out = call_main_doc(doc, [])
        self.assertIn("  2. S2, S3\n     S2 waits on external #40, acme/api#7", out)

    def test_non_ref_unknown_dependency_still_errors(self):
        doc = good()
        for bad in ["S9", "issue 40", "#forty"]:
            with self.subTest(dep=bad):
                doc["slices"][1]["depends_on"] = [bad]
                self.assertIn("unknown-dependency", codes(run(doc)))

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

    def test_shared_files_allow_append_only_overlap(self):
        doc = good()
        doc["slices"][1]["files"].append("CHANGELOG.md")
        doc["slices"][2]["files"].append("CHANGELOG.md")
        r = run(doc)
        self.assertIn("file-overlap", codes(r))
        self.assertIn("shared_files", r["errors"][0]["message"])
        doc["shared_files"] = ["CHANGELOG.md"]
        r = run(doc)
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["overlaps"], [])

    def test_shared_files_do_not_hide_other_overlaps(self):
        doc = good()
        doc["shared_files"] = ["CHANGELOG.md", "docs/notes/"]
        doc["slices"][1]["files"] += ["CHANGELOG.md", "docs/"]
        doc["slices"][2]["files"] += ["CHANGELOG.md", "docs/notes/today.md"]
        r = run(doc)
        self.assertEqual(r["overlaps"], [{"slices": ["S2", "S3"], "files": ["docs/"]}])

    def test_shared_files_must_be_a_list(self):
        doc = good()
        doc["shared_files"] = "CHANGELOG.md"
        self.assertIn("schema", codes(run(doc)))

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


class OversizeTests(unittest.TestCase):
    def oversized(self):
        doc = good()
        s = doc["slices"][2]
        s["scope"] = ["Count requests per email per hour",
                      "Return 429 after 5; also log the email and show a banner",
                      "x" * 210, "Config flag", "Docs"]
        base = s["criteria"][0]
        s["criteria"] = [dict(base, id=f"S3.{i}", covers=["AC4"] if i == 1 else [])
                         for i in range(1, 6)]
        s["criteria"][4]["text"] = "Limit applies per email; the counter resets hourly"
        s["files"] = ([f"config/limit{i}.ts" for i in range(4)] + [f"src/limits/c{i}.ts" for i in range(4)]
                      + [f"src/phases/p{i}.ts" for i in range(3)] + [f"prompts/r{i}.md" for i in range(3)]
                      + ["docs/limits.md", "docs/faq.md"])
        return doc

    def test_oversized_slice_passes_with_warnings(self):
        r = run(self.oversized())
        self.assertTrue(r["ok"], r["errors"])
        w = [x for x in r["warnings"] if x["slice"] == "S3"]
        self.assertIn("many-files", [x["code"] for x in w])
        packed = [x["message"] for x in w if x["code"] == "packed-item"]
        self.assertEqual(len(packed), 3, packed)
        self.assertTrue(any("scope item 2" in m and ";" in m for m in packed))
        self.assertTrue(any("scope item 3 is 210 chars" in m for m in packed))
        self.assertTrue(any("criterion S3.5" in m for m in packed))
        self.assertEqual(r["sizes"]["S3"], {"files": 16, "directories": 5})

    def test_ten_files_is_fine(self):
        doc = good()
        doc["slices"][2]["files"] = [f"src/limits/c{i}.ts" for i in range(10)]
        self.assertNotIn("many-files", codes(run(doc), "warnings"))

    def test_size_line_per_slice(self):
        _, out = call_main_doc(self.oversized(), [])
        self.assertIn("  S1: touches 3 files across 2 directories", out)
        self.assertIn("  S2: touches 1 file across 1 directory", out)
        self.assertIn("  S3: touches 16 files across 5 directories", out)


class SchemaFieldTests(unittest.TestCase):
    def test_empty_out_of_scope_warns(self):
        doc = good()
        doc["slices"][1]["out_of_scope"] = []
        del doc["slices"][2]["out_of_scope"]
        r = run(doc)
        self.assertTrue(r["ok"])
        self.assertEqual([(w["code"], w["slice"]) for w in r["warnings"]],
                         [("no-out-of-scope", "S2"), ("no-out-of-scope", "S3")])

    def test_wrong_types_are_schema_errors(self):
        doc = good()
        doc["slices"][0]["out_of_scope"] = "everything else"
        doc["slices"][1]["context"] = ["not", "a", "string"]
        self.assertEqual(codes(run(doc)).count("schema"), 2)


class DroppedTests(unittest.TestCase):
    def doc(self):
        doc = good()
        doc["parent"]["dropped"] = [
            {"text": "Reset by SMS", "reason": "no SMS provider yet", "covered_by": "#40"},
            {"text": "Remember this device", "reason": "out of scope for this ask"},
        ]
        return doc

    def test_dropped_accepted_and_reported(self):
        r = run(self.doc())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["dropped"][0], {"text": "Reset by SMS", "reason": "no SMS provider yet",
                                           "covered_by": "#40"})
        self.assertEqual(r["dropped"][1]["covered_by"], "")

    def test_dropped_printed(self):
        _, out = call_main_doc(self.doc(), [])
        self.assertIn("dropped from the parent (no slice):\n"
                      "  - Reset by SMS: no SMS provider yet (covered by #40)\n"
                      "  - Remember this device: out of scope for this ask\n", out)

    def test_dropped_needs_text_and_reason(self):
        doc = good()
        doc["parent"]["dropped"] = [{"text": "Reset by SMS"}, {"reason": "why"}]
        c = codes(run(doc))
        self.assertIn("dropped-without-reason", c)
        self.assertIn("schema", c)

    def test_uncovered_message_points_at_dropped(self):
        doc = good()
        doc["slices"][2]["criteria"][0]["covers"] = []
        msg = [e["message"] for e in run(doc)["errors"] if e["code"] == "uncovered"][0]
        self.assertIn("parent.dropped", msg)


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
