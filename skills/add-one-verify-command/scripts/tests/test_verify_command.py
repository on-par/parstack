import importlib.util
import io
import json
import os
import shlex
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
    spec = importlib.util.spec_from_file_location(f"aovc_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


make_verify = _load("make_verify")
prove_verify = _load("prove_verify")

PY = shlex.quote(sys.executable)


def quiet(fn, *args, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = fn(*args, **kwargs)
    return code, out.getvalue()


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fixture(self, name):
        dest = self.tmp / name
        shutil.copytree(FIXTURES / name, dest)
        return dest


class TestMakeVerify(TempDirCase):
    def test_node_pnpm_dry_run_then_write(self):
        root = self.fixture("node-pnpm")
        before = (root / "package.json").read_text()
        code, out = quiet(make_verify.main, [str(root), "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["action"], "would-write")
        self.assertEqual(data["target"], "package.json")
        self.assertEqual(data["content"],
                         "pnpm run lint && pnpm run typecheck && pnpm run build && pnpm run test")
        self.assertEqual((root / "package.json").read_text(), before, "dry run must not write")

        code, _ = quiet(make_verify.main, [str(root), "--write"])
        self.assertEqual(code, 0)
        text = (root / "package.json").read_text()
        pkg = json.loads(text)
        self.assertEqual(pkg["scripts"]["verify"], data["content"])
        self.assertIn('\n    "name"', text, "keeps 4-space indent")

        code, out = quiet(make_verify.main, [str(root), "--json"])
        self.assertEqual(json.loads(out)["action"], "exists")

    def test_npm_placeholder_test_is_skipped(self):
        root = self.tmp / "r"
        root.mkdir()
        (root / "package.json").write_text(json.dumps({"scripts": {
            "build": "tsc", "test": 'echo "Error: no test specified" && exit 1'}}))
        (root / "package-lock.json").write_text("{}")
        result = make_verify.propose(root)
        self.assertEqual([s["cmd"] for s in result["steps"]], ["npm run build"])

    def test_dotnet_writes_verify_sh(self):
        root = self.fixture("dotnet-xunit")
        code, out = quiet(make_verify.main, [str(root), "--write", "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["target"], "scripts/verify.sh")
        script = root / "scripts" / "verify.sh"
        text = script.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", text)
        self.assertIn("step dotnet build App.sln --nologo", text)
        self.assertIn("step dotnet test App.sln --nologo --no-build", text)
        self.assertTrue(os.access(script, os.X_OK))

    def test_python_and_go(self):
        root = self.tmp / "r"
        (root / "tests").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n[tool.pytest.ini_options]\n")
        (root / "go.mod").write_text("module example.com/x\n")
        result = make_verify.propose(root)
        cmds = [s["cmd"] for s in result["steps"]]
        self.assertEqual(result["stacks"], ["python", "go"])
        self.assertEqual(cmds, ["ruff check .", "python3 -m pytest -q",
                                "go build ./...", "go vet ./...", "go test ./..."])
        result = make_verify.propose(root, ["python"])
        self.assertEqual(result["target"], "scripts/verify.sh")

    def test_unknown_stack_exits_1(self):
        code, out = quiet(make_verify.main, [str(self.tmp), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["action"], "nothing")

    def test_bad_path_exits_2(self):
        code, _ = quiet(make_verify.main, [str(self.tmp / "missing")])
        self.assertEqual(code, 2)


class TestPlanting(TempDirCase):
    def test_node_break_next_to_existing_spec(self):
        root = self.tmp / "r"
        (root / "src").mkdir(parents=True)
        (root / "package.json").write_text("{}")
        (root / "src" / "a.spec.ts").write_text("")
        rel, body, stack = prove_verify.auto_break(root)
        self.assertEqual((rel, stack), (os.path.join("src", "zz-verify-break.spec.ts"), "node"))
        self.assertIn("planted break", body)

    def test_dotnet_break_in_test_project(self):
        root = self.fixture("dotnet-xunit")
        rel, body, stack = prove_verify.auto_break(root)
        self.assertEqual(stack, "dotnet")
        self.assertEqual(rel, os.path.join("tests", "App.Tests", "ZzVerifyBreakTest.cs"))
        self.assertIn("[Xunit.Fact]", body)

    def test_go_break_uses_package_name(self):
        root = self.tmp / "r"
        (root / "pkg").mkdir(parents=True)
        (root / "go.mod").write_text("module x\n")
        (root / "pkg" / "a_test.go").write_text("package widgets\n")
        rel, body, _ = prove_verify.auto_break(root)
        self.assertEqual(rel, os.path.join("pkg", "zz_verify_break_test.go"))
        self.assertIn("package widgets", body)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestProve(TempDirCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "repo"
        (self.root / "tests").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (self.root / ".gitignore").write_text("__pycache__/\n")
        (self.root / "tests" / "test_ok.py").write_text(
            "import unittest\n\n\nclass Ok(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")
        (self.root / "notes.txt").write_text("keep me")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
        self.cmd = f"{PY} -m unittest discover -s tests"

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def status(self):
        return subprocess.run(["git", "-C", str(self.root), "status", "--porcelain", "--untracked-files=all"],
                              capture_output=True, text=True).stdout

    def test_auto_break_is_caught_and_reverted(self):
        code, out = quiet(prove_verify.main, [str(self.root), "--cmd", self.cmd, "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0, data)
        self.assertTrue(data["proven"])
        self.assertEqual(data["break"]["source"], "python")
        self.assertEqual([r["passed"] for r in data["runs"].values()], [True, False, True])
        self.assertTrue(data["clean_after_revert"])
        self.assertEqual(self.status(), "")
        self.assertFalse((self.root / "tests" / "test_zz_verify_break.py").exists())

    def test_verify_that_never_fails_is_not_proven(self):
        code, out = quiet(prove_verify.main, [str(self.root), "--cmd", f"{PY} -c pass", "--json"])
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(data["proven"])
        self.assertIn("still passes", data["reason"])
        self.assertEqual(self.status(), "")

    def test_failing_baseline_stops_early(self):
        code, out = quiet(prove_verify.main, [str(self.root), "--cmd", f"{PY} -c 'raise SystemExit(3)'", "--json"])
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(list(data["runs"]), ["clean"])
        self.assertIn("clean tree", data["reason"])

    def test_user_break_restores_exact_bytes(self):
        target = self.root / "tests" / "test_ok.py"
        original = target.read_bytes()
        patch = self.tmp / "break.txt"
        patch.write_text("\nclass Broken(unittest.TestCase):\n    def test_b(self):\n        self.fail('x')\n")
        code, out = quiet(prove_verify.main, [str(self.root), "--cmd", self.cmd, "--break-file",
                                              "tests/test_ok.py", "--break-patch", f"@{patch}", "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["break"]["source"], "user")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.status(), "")

    def test_detects_existing_verify_script(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "verify.sh").write_text(f"set -e\n{self.cmd}\n")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "verify")
        code, out = quiet(prove_verify.main, [str(self.root), "--json"])
        data = json.loads(out)
        self.assertEqual(data["verify_command"], "bash scripts/verify.sh")
        self.assertEqual(code, 0, data)

    def test_usage_errors(self):
        self.assertEqual(quiet(prove_verify.main, [str(self.root), "--cmd", "x", "--break-file", "a"])[0], 2)
        self.assertEqual(quiet(prove_verify.main, [str(self.root)])[0], 2)  # no verify command
        outside = ["--cmd", self.cmd, "--break-file", "../escape.txt", "--break-patch", "x"]
        self.assertEqual(quiet(prove_verify.main, [str(self.root), *outside])[0], 2)
        self.assertEqual(quiet(prove_verify.main, [str(self.tmp), "--cmd", self.cmd])[0], 2)  # not a git repo


if __name__ == "__main__":
    unittest.main()
