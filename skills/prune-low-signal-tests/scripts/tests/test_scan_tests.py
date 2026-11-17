import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scan_tests.py")

_spec = importlib.util.spec_from_file_location("parstack_scan_tests", SCRIPT)
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = st.main(argv)
    return code, out.getvalue(), err.getvalue()


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(text))


IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class TmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name


class TestFilePatterns(unittest.TestCase):
    def test_common_test_file_names_are_recognised(self):
        for path in ["a/b.test.ts", "a/b.spec.jsx", "x.test.mjs", "a/__tests__/b.js", "test_a.py",
                     "pkg/a_test.py", "cmd/a_test.go", "App.Tests/WidgetTests.cs", "src/FooTest.java",
                     "spec/user_spec.rb", "test/a_test.exs"]:
            self.assertTrue(st.is_test_file(path), path)

    def test_source_and_helper_files_are_not_tests(self):
        for path in ["src/test.ts", "conftest.py", "a/testing.go", "src/contest_a.py", "a/latest.test",
                     "tests/helpers.py", "README.test.md"]:
            self.assertFalse(st.is_test_file(path), path)


class TestList(TmpDir):
    def test_groups_by_leading_dirs_and_counts_tests(self):
        write(self.root, "packages/api/src/a.test.ts", """\
            describe('a', () => {
              it('one', () => {});
              test.skip('two', () => {});
            });
            """)
        write(self.root, "packages/api/b_test.go", "func TestB(t *testing.T) {}\n")
        write(self.root, "tools/x/Thing.Tests.cs", "class ThingTests {}\n")
        code, out, _ = run_main(["list", self.root, "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([a["area"] for a in data["areas"]], ["packages/api", "tools/x"])
        api = data["areas"][0]
        self.assertEqual((api["file_count"], api["tests"]), (2, 3))
        self.assertEqual(data["areas"][1]["files"][0]["tests"], None)
        self.assertEqual((data["total_files"], data["total_tests"]), (3, 3))

    def test_depth_one_merges_packages(self):
        write(self.root, "packages/api/a.test.ts", "it('a', () => {});\n")
        write(self.root, "packages/web/b.test.ts", "it('b', () => {});\n")
        _, out, _ = run_main(["list", self.root, "--depth", "1", "--json"])
        self.assertEqual([(a["area"], a["file_count"]) for a in json.loads(out)["areas"]], [("packages", 2)])

    def test_git_ignored_tests_are_skipped(self):
        write(self.root, ".gitignore", "generated/\n")
        write(self.root, "generated/a.test.ts", "it('a', () => {});\n")
        write(self.root, "src/b.test.ts", "it('b', () => {});\n")
        git(self.root, "init", "-q")
        _, out, _ = run_main(["list", self.root, "--json"])
        paths = [f["path"] for a in json.loads(out)["areas"] for f in a["files"]]
        self.assertEqual(paths, ["src/b.test.ts"])

    def test_human_output_names_each_area(self):
        write(self.root, "src/a/b.test.ts", "it('a', () => {});\nit('b', () => {});\n")
        code, out, _ = run_main(["list", self.root])
        self.assertEqual(code, 0)
        self.assertIn("src/a", out)
        self.assertIn("2 test(s)", out)

    def test_output_is_identical_across_runs(self):
        for name in ["z", "a", "m"]:
            write(self.root, f"{name}/x/{name}.test.ts", "it('a', () => {});\n")
        self.assertEqual(run_main(["list", self.root, "--json"]), run_main(["list", self.root, "--json"]))


PLAIN_DIFF = """\
--- a/src/a.test.ts\t2026-01-01
+++ b/src/a.test.ts\t2026-01-02
@@ -1,3 +1,3 @@
-it('old name', () => {
+it('new name', () => {
   expect(f()).toBe(1);
 });
--- a/src/b.test.ts
+++ b/src/b.test.ts
@@ -1,2 +1,3 @@
 it('kept', () => {});
+it('added', () => {});
 // end
"""


class TestDiff(TmpDir):
    def test_plain_unified_diff_with_several_files(self):
        result = st.run_diff(PLAIN_DIFF, "x")
        by_path = {e["path"]: e for e in result["test_files"]}
        self.assertEqual(sorted(by_path), ["src/a.test.ts", "src/b.test.ts"])
        self.assertEqual([d["name"] for d in by_path["src/a.test.ts"]["tests_removed"]], ["old name"])
        self.assertEqual([d["name"] for d in by_path["src/a.test.ts"]["tests_added"]], ["new name"])
        self.assertEqual([(d["name"], d["line"]) for d in by_path["src/b.test.ts"]["tests_added"]], [("added", 2)])

    def test_edited_declaration_with_same_name_is_not_added_or_removed(self):
        diff = textwrap.dedent("""\
            diff --git a/t_test.py b/t_test.py
            --- a/t_test.py
            +++ b/t_test.py
            @@ -1,2 +1,2 @@
            -def test_total(self):
            +def test_total(self, fixture):
                 assert total() == 3
            """)
        entry = st.run_diff(diff, "x")["test_files"][0]
        self.assertEqual((entry["tests_added"], entry["tests_removed"]), ([], []))

    def test_removed_line_that_looks_like_a_header_stays_in_its_hunk(self):
        diff = textwrap.dedent("""\
            --- a/x_test.go
            +++ b/x_test.go
            @@ -1,2 +1,1 @@
            --- not a header
             func TestX(t *testing.T) {}
            """)
        files = st.parse_diff(diff)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["removed"], [(1, "-- not a header")])

    def test_non_test_changes_only_report_deleted_or_moved_sources(self):
        diff = textwrap.dedent("""\
            diff --git a/src/a.ts b/src/a.ts
            --- a/src/a.ts
            +++ b/src/a.ts
            @@ -1 +1 @@
            -export const a = 1;
            +export const a = 2;
            diff --git a/src/old.ts b/src/new.ts
            similarity index 100%
            rename from src/old.ts
            rename to src/new.ts
            """)
        result = st.run_diff(diff, "x")
        self.assertEqual(result["test_files"], [])
        self.assertEqual(result["moved_or_deleted_source_files"],
                         [{"path": "src/old.ts", "status": "renamed", "new_path": "src/new.ts"}])

    def test_reads_diff_from_stdin(self):
        proc = subprocess.run([sys.executable, SCRIPT, "diff", "--diff-file", "-", "--json"],
                              input=PLAIN_DIFF, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["source"], "stdin")
        self.assertEqual(data["totals"], {"test_files": 2, "tests_added": 2, "tests_removed": 1})

    def test_base_ref_reads_git_history(self):
        write(self.root, "a.test.js", "test('one', () => {});\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, *IDENTITY, "commit", "-qm", "base")
        git(self.root, "checkout", "-qb", "feature")
        write(self.root, "a.test.js", "test('one', () => {});\ntest('two', () => {});\n")
        git(self.root, *IDENTITY, "commit", "-qam", "head")
        code, out, _ = run_main(["diff", "--base", "main", "--repo", self.root, "--json"])
        self.assertEqual(code, 0)
        added = json.loads(out)["test_files"][0]["tests_added"]
        self.assertEqual([(d["name"], d["line"]) for d in added], [("two", 2)])

    def test_unknown_base_ref_is_a_usage_error(self):
        git(self.root, "init", "-q")
        code, _, err = run_main(["diff", "--base", "no-such-ref", "--repo", self.root])
        self.assertEqual(code, 2)
        self.assertIn("no-such-ref", err)

    def test_missing_diff_file_is_a_usage_error(self):
        code, _, err = run_main(["diff", "--diff-file", os.path.join(self.root, "nope.diff")])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_human_output_lists_added_and_removed_names(self):
        path = os.path.join(self.root, "c.diff")
        write(self.root, "c.diff", PLAIN_DIFF)
        code, out, _ = run_main(["diff", "--diff-file", path])
        self.assertEqual(code, 0)
        self.assertIn('+ test "new name"', out)
        self.assertIn('- test "old name"', out)


class TestSmells(TmpDir):
    def kinds(self, line):
        return [h["kind"] for h in st.smells_in(line + "\n")]

    def test_each_hint_kind_fires_on_its_pattern(self):
        cases = {
            "expect(fs.existsSync('a.ts')).toBe(false);": "absence-assertion",
            "assert not hasattr(mod, 'old_flag')": "absence-assertion",
            "expect(total(items)).toEqual(total(items));": "self-comparison",
            "assert compute(1) == compute(1)": "self-comparison",
            "expect(save).toHaveBeenCalledTimes(2);": "call-count",
            "mock.assert_called_once_with(1)": "call-count",
            "expect((svc as any).queue.length).toBe(0);": "private-access",
            "assert worker._pending == []": "private-access",
            "if (typeof api.load === 'function') {}": "type-only-check",
            "expect(api.load).toBeInstanceOf(Function);": "type-only-check",
            "@pytest.mark.skipif(sys.version_info < (3, 0), reason='py2')": "skipped-or-gated",
            "xit('old path', () => {});": "skipped-or-gated",
            "expect(el).toHaveClass('rounded-md border px-3 py-1 text-xs');": "copied-class-string",
        }
        for line, kind in cases.items():
            self.assertIn(kind, self.kinds(line), line)

    def test_ordinary_assertions_are_not_flagged(self):
        for line in [
            "expect(total([{ price: 2, qty: 3 }])).toBe(6);",
            "self.assertEqual(len(calls), 3)",
            "self._helper()",
            "it('applies the dark style to the header row in the table', () => {",
            "expect(screen.getByText('shows the sign-in page for new users')).toBeTruthy();",
            "// expect(x).toEqual(x);",
        ]:
            self.assertEqual(self.kinds(line), [], line)

    def test_scans_a_single_file_and_counts_every_kind(self):
        write(self.root, "a.test.ts", "expect(x).toBe(x);\nexpect(f).toHaveBeenCalledTimes(1);\n")
        code, out, _ = run_main(["smells", os.path.join(self.root, "a.test.ts"), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["files_scanned"], 1)
        self.assertEqual(data["files"][0]["path"], "a.test.ts")
        self.assertEqual(data["hint_counts"]["self-comparison"], 1)
        self.assertEqual(data["hint_counts"]["call-count"], 1)
        self.assertEqual(data["hint_counts"]["private-access"], 0)

    def test_non_test_files_are_not_scanned(self):
        write(self.root, "src/a.ts", "expect(x).toBe(x);\n")
        _, out, _ = run_main(["smells", self.root, "--json"])
        self.assertEqual(json.loads(out)["files_scanned"], 0)

    def test_human_output_says_hints_are_not_verdicts(self):
        write(self.root, "a_test.py", "def test_a():\n    assert f() == f()\n")
        code, out, _ = run_main(["smells", self.root])
        self.assertEqual(code, 0)
        self.assertIn("self-comparison", out)
        self.assertIn("not verdicts", out)


class TestUsage(TmpDir):
    def test_missing_path_is_a_usage_error(self):
        code, _, err = run_main(["list", os.path.join(self.root, "missing")])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_json_errors_are_machine_readable(self):
        code, out, _ = run_main(["smells", os.path.join(self.root, "missing"), "--json"])
        self.assertEqual(code, 2)
        data = json.loads(out)
        self.assertEqual((data["mode"], data["status"]), ("smells", "error"))
        self.assertIn("not found", data["message"])

    def test_list_needs_a_directory(self):
        write(self.root, "a.test.ts", "it('a', () => {});\n")
        code, _, _ = run_main(["list", os.path.join(self.root, "a.test.ts")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
