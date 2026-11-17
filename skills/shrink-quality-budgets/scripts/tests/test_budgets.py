import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def _load(name):
    path = HERE.parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"sqb_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


budgets = _load("budgets")


def quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = fn(*args, **kwargs)
    return code, out.getvalue()


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def no_tools():
    def run(*a, **k):
        raise AssertionError("unexpected external call")
    return budgets.Env(which=lambda n: None, run=run)


class TestCounting(unittest.TestCase):
    def test_typescript_sample(self):
        counts = budgets.count_text((FIXTURES / "sample.ts").read_text(), ".ts")
        self.assertEqual(counts, {"loc": 7, "comment_lines": 8})

    def test_python_sample(self):
        counts = budgets.count_text((FIXTURES / "sample.py").read_text(), ".py")
        self.assertEqual(counts, {"loc": 4, "comment_lines": 2})

    def test_csharp_block(self):
        text = "/// <summary>doc</summary>\npublic class A\n{\n    /* x */ int b; // c\n}\n"
        self.assertEqual(budgets.count_text(text, ".cs"), {"loc": 4, "comment_lines": 1})


class TestGlobs(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(budgets.matches("src/a/b.ts", ["src/**"]))
        self.assertTrue(budgets.matches("src/b.ts", ["src/**/*.ts"]))
        self.assertTrue(budgets.matches("deep/x/y.test.ts", ["*.test.ts"]))
        self.assertTrue(budgets.matches("a/generated/x.cs", ["**/generated/**"]))
        self.assertTrue(budgets.matches("vendor/x.go", ["vendor/"]))
        self.assertTrue(budgets.matches("src/x.ts", ["./src/**"]))
        self.assertFalse(budgets.matches("lib/a.ts", ["src/**"]))
        self.assertFalse(budgets.matches("src/a.tsx", ["src/*.ts"]))


class TempRepo(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.write("src/app.ts", "const a = 1;\n// note\nexport default a;\n")
        self.write("src/app.test.ts", "test('a', () => {});\n")
        self.write("tools/gen.py", "# tool\nprint(1)\n")
        self.write("README.md", "# not code\n")

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def config(self, **kw):
        data = {"include": [], "exclude": [], "budgets": {}, "sources": {}}
        data.update(kw)
        self.write("quality-budgets.json", json.dumps(data))
        return data

    def main(self, *args, env=None):
        return quiet(budgets.main, [*args, "--root", str(self.root), "--json"], env=env or no_tools())


class TestMeasure(TempRepo):
    def test_walk_without_git(self):
        code, out = self.main("measure")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["files"], 3)
        self.assertEqual(data["metrics"]["loc"]["value"], 4)
        self.assertEqual(data["metrics"]["comment_lines"]["value"], 2)
        for name in ("duplication", "dead_code", "boundary_violations"):
            self.assertEqual(data["metrics"][name]["status"], "skipped")
            self.assertIn("skipped", data["metrics"][name]["reason"])

    def test_include_exclude(self):
        self.config(include=["src/**"], exclude=["*.test.ts"])
        code, out = self.main("measure")
        data = json.loads(out)
        self.assertEqual(data["files"], 1)
        self.assertEqual(data["metrics"]["loc"]["value"], 2)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_gitignore_respected(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "tools/\n")
        env = budgets.Env(which=lambda n: shutil.which(n) if n == "git" else None)
        code, out = self.main("measure", env=env)
        self.assertEqual(json.loads(out)["files"], 2)

    def test_reports(self):
        shutil.copy(FIXTURES / "knip-report.json", self.root / "knip.json")
        shutil.copy(FIXTURES / "jscpd-report.json", self.root / "jscpd.json")
        self.write("boundaries-baseline.json", json.dumps({"count": 3, "violations": [{}, {}, {}]}))
        self.config(sources={"knip_report": "knip.json", "jscpd_report": "jscpd.json"})
        code, out = self.main("measure")
        m = json.loads(out)["metrics"]
        self.assertEqual(m["dead_code"]["value"], 7)
        self.assertEqual(m["duplication"]["value"], 4.2)
        self.assertEqual(m["boundary_violations"]["value"], 3)
        self.assertEqual(m["boundary_violations"]["source"], "boundaries-baseline.json")

    def test_depcruise_report_wins_over_baseline(self):
        report = {"summary": {"violations": [{"rule": {"severity": "error", "name": "x"}},
                                             {"rule": {"severity": "info", "name": "y"}}]}}
        self.write("dc.json", json.dumps(report))
        self.write("boundaries-baseline.json", json.dumps({"count": 9}))
        self.config(sources={"depcruise_report": "dc.json"})
        _, out = self.main("measure")
        self.assertEqual(json.loads(out)["metrics"]["boundary_violations"]["value"], 1)

    def test_tool_runs_are_mocked(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv[0])
            if argv[0] == "/x/jscpd":
                out_dir = Path(argv[argv.index("--output") + 1])
                shutil.copy(FIXTURES / "jscpd-report.json", out_dir / "jscpd-report.json")
                return FakeProc(0)
            if argv[0] == "/x/knip":
                return FakeProc(1, (FIXTURES / "knip-report.json").read_text())
            raise AssertionError(argv)

        env = budgets.Env(which=lambda n: f"/x/{n}" if n in ("jscpd", "knip") else None, run=run)
        _, out = self.main("measure", env=env)
        m = json.loads(out)["metrics"]
        self.assertEqual((m["duplication"]["value"], m["dead_code"]["value"]), (4.2, 7))
        self.assertEqual(sorted(calls), ["/x/jscpd", "/x/knip"])


class TestCheck(TempRepo):
    def test_missing_config_is_usage_error(self):
        self.assertEqual(self.main("check")[0], 2)

    def test_bad_json_is_usage_error(self):
        self.write("quality-budgets.json", "{nope")
        self.assertEqual(self.main("check")[0], 2)

    def test_pass_fail_and_skipped(self):
        self.config(budgets={"loc": 4, "dead_code": 0})
        code, out = self.main("check")
        data = json.loads(out)
        self.assertEqual(code, 0)
        rows = {r["metric"]: r for r in data["rows"]}
        self.assertEqual(rows["loc"]["status"], "ok")
        self.assertEqual(rows["dead_code"]["status"], "skipped", "missing tool must not fail")
        self.assertEqual(rows["comment_lines"]["status"], "no-budget")

        self.config(budgets={"loc": 3})
        code, out = self.main("check")
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(data["over"], ["loc"])
        self.assertIn("deliberate manual edit", data["note"])

    def test_tighten_lowers_never_raises(self):
        self.config(budgets={"loc": 100, "comment_lines": 1})
        code, out = self.main("check", "--tighten")
        data = json.loads(out)
        self.assertEqual(code, 1, "comment_lines is over budget")
        self.assertEqual(data["tightened"], [{"metric": "loc", "from": 100, "to": 4}])
        saved = json.loads((self.root / "quality-budgets.json").read_text())
        self.assertEqual(saved["budgets"], {"loc": 4, "comment_lines": 1})

    def test_init_then_check_passes(self):
        code, out = self.main("init")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["budgets"], {"loc": 4, "comment_lines": 2})
        self.assertIn("dead_code", data["skipped"])
        self.assertEqual(self.main("check")[0], 0)
        code, out = self.main("init")
        self.assertEqual(json.loads(out)["action"], "exists")

    def test_human_output(self):
        self.config(budgets={"loc": 3})
        code, out = quiet(budgets.main, ["check", "--root", str(self.root)], env=no_tools())
        self.assertEqual(code, 1)
        self.assertIn("FAIL (1 over budget)", out)
        self.assertIn("over (+1)", out)


if __name__ == "__main__":
    unittest.main()
