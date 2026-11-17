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
    spec = importlib.util.spec_from_file_location(f"aar_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


assess = _load("assess")


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def no_tools_env(run=None, clock=None):
    """Env with no external tools: forces the walk-based file list and regex secret scan."""
    def which(name):
        return None

    def default_run(*a, **k):
        raise AssertionError("unexpected external call")

    kwargs = {"which": which, "run": run or default_run}
    if clock:
        kwargs["clock"] = clock
    return assess.Env(**kwargs)


def copy_fixture(name):
    tmp = tempfile.mkdtemp()
    dest = Path(tmp) / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


class TestReadyTs(unittest.TestCase):
    def setUp(self):
        self.root = copy_fixture("ready-ts")
        # CI config dir is created here so the fixture tree stays free of dot-dirs.
        wf = self.root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        (self.root / ".dependency-cruiser.cjs").write_text("module.exports = {forbidden: []};\n")

    def tearDown(self):
        shutil.rmtree(self.root.parent)

    def test_full_score(self):
        report = assess.assess(self.root, env=no_tools_env())
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(report["stack"], "typescript")
        self.assertEqual(report["verify_command"], "pnpm run verify")
        self.assertEqual(by_id["verify_runs"]["status"], "skipped")
        self.assertEqual(by_id["boundaries"]["status"], "pass")
        self.assertEqual(by_id["dead_code"]["status"], "pass")
        self.assertEqual(by_id["duplication"]["status"], "pass")
        self.assertEqual(by_id["agent_context"]["status"], "pass")
        self.assertEqual(by_id["ci"]["status"], "pass")
        self.assertEqual(by_id["tests"]["status"], "pass")
        # secrets: no gitleaks, regex clean -> warn
        self.assertEqual(by_id["secrets"]["status"], "warn")
        self.assertGreaterEqual(report["score"], 90)
        self.assertEqual([f["id"] for f in report["fixes"]], ["secrets"])

    def test_runs_flaky(self):
        results = iter([0, 1, 0])
        ticks = iter(range(100))

        def run(cmd, **kw):
            self.assertEqual(cmd, ["pnpm", "run", "verify"])
            return FakeProc(next(results))

        env = no_tools_env(run=run, clock=lambda: next(ticks))
        report = assess.assess(self.root, runs=3, env=env)
        runs = {c["id"]: c for c in report["checks"]}["verify_runs"]
        self.assertEqual(runs["status"], "fail")
        self.assertIn("flaky", runs["detail"])

    def test_runs_slow(self):
        ticks = iter([0, 900, 900, 1800])
        env = no_tools_env(run=lambda cmd, **kw: FakeProc(0), clock=lambda: next(ticks))
        report = assess.assess(self.root, runs=2, slow_seconds=600, env=env)
        runs = {c["id"]: c for c in report["checks"]}["verify_runs"]
        self.assertEqual(runs["status"], "warn")


class TestBareRepo(unittest.TestCase):
    def setUp(self):
        self.root = copy_fixture("bare-dotnet")

    def tearDown(self):
        shutil.rmtree(self.root.parent)

    def test_dotnet_detection_and_ranking(self):
        report = assess.assess(self.root, env=no_tools_env())
        by_id = {c["id"]: c for c in report["checks"]}
        self.assertEqual(report["stack"], "dotnet")
        # a solution counts as a single verify command
        self.assertEqual(report["verify_command"], "dotnet test App.sln")
        self.assertEqual(by_id["boundaries"]["status"], "fail")
        self.assertEqual(by_id["agent_context"]["status"], "fail")
        self.assertEqual(by_id["ci"]["status"], "fail")
        self.assertEqual(by_id["dead_code"]["status"], "warn")
        # boundaries (15) must outrank ci/agent_context (10)
        self.assertEqual(report["fixes"][0]["id"], "boundaries")
        impacts = [f["impact"] for f in report["fixes"]]
        self.assertEqual(impacts, sorted(impacts, reverse=True))
        self.assertLess(report["score"], 60)

    def test_planted_secret_is_found(self):
        key = "AKIA" + "ABCDEFGHIJKLMNOP"  # built at runtime so the repo holds no literal key
        (self.root / "config.txt").write_text(f"key = {key}\n")
        report = assess.assess(self.root, env=no_tools_env())
        secrets = {c["id"]: c for c in report["checks"]}["secrets"]
        self.assertEqual(secrets["status"], "fail")
        self.assertIn("config.txt:1", secrets["detail"])

    def test_architecture_reference_passes(self):
        proj = self.root / "src" / "App" / "App.csproj"
        proj.write_text(proj.read_text().replace(
            "</Project>", '  <ItemGroup><PackageReference Include="TngTech.ArchUnitNET.xUnit" /></ItemGroup>\n</Project>'))
        report = assess.assess(self.root, env=no_tools_env())
        self.assertEqual({c["id"]: c for c in report["checks"]}["boundaries"]["status"], "pass")


class TestUnknownStack(unittest.TestCase):
    def test_empty_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess.assess(Path(tmp), env=no_tools_env())
            by_id = {c["id"]: c for c in report["checks"]}
            self.assertEqual(report["stack"], "unknown")
            self.assertEqual(by_id["boundaries"]["status"], "skipped")
            self.assertEqual(by_id["dead_code"]["status"], "skipped")
            self.assertEqual(by_id["verify_command"]["status"], "fail")
            self.assertEqual(report["fixes"][0]["id"], "verify_command")

    def test_fixture_projects_do_not_set_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp, "pkg", "tests", "fixtures", "app")
            deep.mkdir(parents=True)
            (deep / "App.sln").write_text("")
            (deep / "App.csproj").write_text("<Project />")
            report = assess.assess(Path(tmp), env=no_tools_env())
            self.assertEqual(report["stack"], "unknown")
            self.assertIsNone(report["verify_command"])

    def test_makefile_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Makefile").write_text("build:\n\tgo build\nverify: build\n\tgo test ./...\n")
            report = assess.assess(Path(tmp), env=no_tools_env())
            self.assertEqual(report["verify_command"], "make verify")


class TestGitAndGitleaks(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_ls_files_respects_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("secret.env\n")
            key = "AKIA" + "ABCDEFGHIJKLMNOP"
            (root / "secret.env").write_text(f"k={key}\n")
            env = assess.Env(which=lambda n: shutil.which(n) if n == "git" else None)
            files = assess.list_files(root, env)
            self.assertIn(".gitignore", files)
            self.assertNotIn("secret.env", files)

    def test_gitleaks_findings(self):
        def run(cmd, **kw):
            self.assertEqual(cmd[0], "gitleaks")
            report = cmd[cmd.index("--report-path") + 1]
            Path(report).write_text(json.dumps([{"RuleID": "x"}, {"RuleID": "y"}]))
            return FakeProc(1)

        env = assess.Env(which=lambda n: "/bin/gitleaks" if n == "gitleaks" else None, run=run)
        with tempfile.TemporaryDirectory() as tmp:
            result = assess.check_secrets(Path(tmp), [], env)
        self.assertEqual(result["status"], "fail")
        self.assertIn("2 finding", result["detail"])

    def test_gitleaks_clean(self):
        env = assess.Env(which=lambda n: "/bin/gitleaks" if n == "gitleaks" else None,
                         run=lambda cmd, **kw: FakeProc(0))
        with tempfile.TemporaryDirectory() as tmp:
            result = assess.check_secrets(Path(tmp), [], env)
        self.assertEqual(result["status"], "pass")


class TestCli(unittest.TestCase):
    def test_json_and_min_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = assess.main([tmp, "--json", "--min-score", "90"], env=no_tools_env())
            data = json.loads(buf.getvalue())
            self.assertEqual(code, 1)
            self.assertIn("score", data)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(assess.main([tmp], env=no_tools_env()), 0)

    def test_bad_path(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(assess.main(["/nonexistent/path/x"], env=no_tools_env()), 2)


if __name__ == "__main__":
    unittest.main()
