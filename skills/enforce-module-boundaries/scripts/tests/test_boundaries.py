import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPORT = FIXTURES / "depcruise-report.json"
DOTNET_OUT = FIXTURES / "dotnet-test-output.txt"


def _load(name):
    path = HERE.parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"emb_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load("gen_boundaries")
chk = _load("check_boundaries")


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
    return chk.Env(which=lambda n: None, run=run)


class TempRepo(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.repo)

    def touch(self, rel, text=""):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class TestGenerateTs(TempRepo):
    def setUp(self):
        super().setUp()
        self.touch("package.json", "{}")
        self.touch("tsconfig.json", "{}")
        for rel in ("src/billing/index.ts", "src/users/index.ts", "src/users/internal/repo.ts",
                    "src/__tests__/a.test.ts", "src/main.ts"):
            self.touch(rel)

    def test_dry_run_lists_modules(self):
        code, out = quiet(gen.main, [str(self.repo), "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["stack"], "ts")
        self.assertEqual(data["modules"], ["src/billing", "src/users"])
        self.assertEqual(data["action"], "would-write")
        self.assertFalse((self.repo / ".dependency-cruiser.cjs").exists())
        content = data["content"]
        self.assertIn('name: "no-circular"', content)
        self.assertIn('"^(src/billing|src/users)/"', content)
        self.assertIn('"^$1/"', content)
        self.assertIn('index\\\\.(ts|tsx|js|jsx|mjs|cjs)$', content)
        self.assertIn('tsConfig: { fileName: "tsconfig.json" }', content)

    def test_regex_matches_intent(self):
        # The pathNot index pattern, unescaped from the JS string, must accept index files only.
        content = gen.render_depcruise(["src/billing", "src/users"], False)
        literal = re.search(r'pathNot: \["\^\$1/", (".*?")\]', content).group(1)
        pattern = json.loads(literal)
        self.assertTrue(re.match(pattern, "src/users/index.ts"))
        self.assertFalse(re.match(pattern, "src/users/internal/repo.ts"))

    def test_write_then_exists(self):
        self.assertEqual(quiet(gen.main, [str(self.repo), "--write"])[0], 0)
        self.assertTrue((self.repo / ".dependency-cruiser.cjs").exists())
        code, out = quiet(gen.main, [str(self.repo), "--json"])
        self.assertEqual((code, json.loads(out)["action"]), (0, "exists"))

    def test_modules_glob_and_too_few(self):
        self.touch("src/features/cart/index.ts")
        self.touch("src/features/checkout/index.ts")
        code, out = quiet(gen.main, [str(self.repo), "--modules-glob", "features/*", "--json"])
        self.assertEqual(json.loads(out)["modules"], ["src/features/cart", "src/features/checkout"])
        code, out = quiet(gen.main, [str(self.repo), "--module", "src/billing", "--json"])
        self.assertEqual((code, json.loads(out)["action"]), (1, "nothing"))

    def test_bad_root(self):
        self.assertEqual(quiet(gen.main, [str(self.repo), "--root", "nope"])[0], 2)


class TestGenerateDotnet(TempRepo):
    def test_folder_modules_in_one_project(self):
        self.touch("App.sln")
        self.touch("src/App/App.csproj", "<Project><PropertyGroup><RootNamespace>Shop.App</RootNamespace>"
                                        "</PropertyGroup></Project>")
        self.touch("src/App/Billing/Invoice.cs")
        self.touch("src/App/Users/User.cs")
        self.touch("src/App/obj/x.cs")
        self.touch("tests/App.Tests/App.Tests.csproj", '<PackageReference Include="xunit" />')
        code, out = quiet(gen.main, [str(self.repo), "--root", "src/App", "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["stack"], "dotnet")
        self.assertEqual(data["namespaces"], ["Shop.App.Billing", "Shop.App.Users"])
        self.assertEqual(data["target"], "tests/App.Tests/ModuleBoundariesTests.cs")
        self.assertIn('"Shop.App.Billing",', data["content"])
        self.assertIn("public class ModuleBoundariesTests", data["content"])
        self.assertIn('LoadFilteredDirectory(System.AppContext.BaseDirectory, "Shop*.dll")', data["content"])
        self.assertIn("NotDependOnAny", data["content"])

    def test_project_per_module(self):
        self.touch("src/Shop.Billing/Shop.Billing.csproj", "<Project />")
        self.touch("src/Shop.Users/Shop.Users.csproj", "<Project />")
        code, out = quiet(gen.main, [str(self.repo), "--stack", "dotnet", "--json"])
        self.assertEqual(json.loads(out)["namespaces"], ["Shop.Billing", "Shop.Users"])


class TestCheckDepcruise(TempRepo):
    def run_check(self, *extra, env=None):
        return quiet(chk.main, [str(self.repo), "--depcruise-json", str(REPORT), "--json", *extra], env=env or no_tools())

    def test_no_baseline_means_all_new(self):
        code, out = self.run_check()
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(data["current_count"], 2, "info severity is ignored")
        self.assertEqual(len(data["new"]), 2)
        self.assertFalse(data["baseline_found"])

    def test_update_then_pass_then_new_and_fixed(self):
        code, out = self.run_check("--update-baseline")
        self.assertEqual(code, 0)
        baseline = json.loads((self.repo / "boundaries-baseline.json").read_text())
        self.assertEqual(baseline["count"], 2)
        self.assertEqual(baseline["tool"], "dependency-cruiser")

        code, out = self.run_check()
        self.assertEqual((code, json.loads(out)["status"]), (0, "pass"))

        # Swap one accepted violation for a different one: one new, one fixed.
        baseline["violations"][0]["to"] = "src/users/gone.ts"
        (self.repo / "boundaries-baseline.json").write_text(json.dumps(baseline))
        code, out = self.run_check()
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(len(data["new"]), 1)
        self.assertEqual(data["fixed"][0]["to"], "src/users/gone.ts")

    def test_fewer_violations_than_baseline_passes(self):
        extra = {"rule": "no-circular", "from": "src/old.ts", "to": "src/older.ts"}
        current = chk.parse_depcruise(json.loads(REPORT.read_text()))
        chk.write_baseline(self.repo / "boundaries-baseline.json", "dependency-cruiser", current + [extra])
        code, out = self.run_check()
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["fixed"], [extra])
        self.assertIn("--update-baseline", data["reason"])

    def test_runs_local_depcruise(self):
        self.touch(".dependency-cruiser.cjs", "module.exports = {};")
        self.touch("node_modules/.bin/depcruise")
        (self.repo / "src").mkdir()
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return FakeProc(1, REPORT.read_text())

        env = chk.Env(which=lambda n: None, run=run)
        code, out = quiet(chk.main, [str(self.repo), "--json"], env=env)
        self.assertEqual(code, 1)
        self.assertTrue(calls[0][0].endswith("node_modules/.bin/depcruise"))
        self.assertEqual(calls[0][1:], ["--config", ".dependency-cruiser.cjs", "--output-type", "json", "src"])

    def test_skipped_without_tool(self):
        self.touch(".dependency-cruiser.cjs", "module.exports = {};")
        code, out = quiet(chk.main, [str(self.repo), "--json"], env=no_tools())
        data = json.loads(out)
        self.assertEqual((code, data["status"]), (0, "skipped"))
        self.assertIn("not found", data["reason"])

    def test_npx_without_package_is_skipped(self):
        self.touch(".dependency-cruiser.cjs", "module.exports = {};")
        env = chk.Env(which=lambda n: "/usr/bin/npx" if n == "npx" else None,
                      run=lambda argv, **kw: FakeProc(1, "", "npm error could not determine executable"))
        code, out = quiet(chk.main, [str(self.repo), "--json"], env=env)
        self.assertEqual(json.loads(out)["status"], "skipped")

    def test_missing_config_is_usage_error(self):
        self.touch("package.json", "{}")
        self.assertEqual(quiet(chk.main, [str(self.repo)], env=no_tools())[0], 2)


class TestCheckDotnet(TempRepo):
    def test_parse_saved_output(self):
        violations = chk.parse_dotnet(DOTNET_OUT.read_text())
        self.assertEqual(violations, [
            {"rule": "module-boundary", "from": "App.Billing", "to": "App.Users"},
            {"rule": "module-boundary", "from": "App.Users", "to": "App.Orders"},
        ])

    def test_runs_dotnet_test_with_filter(self):
        self.touch("App.sln")
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return FakeProc(1, DOTNET_OUT.read_text())

        env = chk.Env(which=lambda n: "/usr/bin/dotnet" if n == "dotnet" else None, run=run)
        code, out = quiet(chk.main, [str(self.repo), "--json", "--update-baseline"], env=env)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ["dotnet", "test", "App.sln", "--nologo", "--filter",
                                    "FullyQualifiedName~ModuleBoundariesTests"])
        self.assertEqual(json.loads((self.repo / "boundaries-baseline.json").read_text())["count"], 2)
        code, out = quiet(chk.main, [str(self.repo), "--json"], env=env)
        self.assertEqual((code, json.loads(out)["status"]), (0, "pass"))

    def test_build_failure_is_error(self):
        self.touch("App.sln")
        env = chk.Env(which=lambda n: "/usr/bin/dotnet", run=lambda argv, **kw: FakeProc(1, "error CS1002: ; expected"))
        code, out = quiet(chk.main, [str(self.repo), "--json"], env=env)
        self.assertEqual((code, json.loads(out)["status"]), (1, "error"))

    def test_skipped_without_dotnet(self):
        self.touch("App.sln")
        code, out = quiet(chk.main, [str(self.repo), "--json"], env=no_tools())
        self.assertEqual((code, json.loads(out)["status"]), (0, "skipped"))


if __name__ == "__main__":
    unittest.main()
