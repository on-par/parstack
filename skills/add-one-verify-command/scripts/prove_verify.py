#!/usr/bin/env python3
"""Prove that verify catches a break.

1. run verify on the clean tree: must pass
2. plant a known-failing test (or --break-file/--break-patch), run verify: must fail
3. revert the break (always, in finally) and check `git status` matches step 1
4. run verify again: must pass

Exit codes: 0 proven, 1 not proven, 2 usage/input error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_stack  # noqa: E402  (sibling module in this scripts dir)

MARK = "planted break: verify must fail"
NODE_TEST_RE = re.compile(r"\.(test|spec)(\.[cm]?[jt]sx?)$")
PY_TEST_RE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
GO_TEST_RE = re.compile(r"_test\.go$")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- planted breaks

def _first(files: list[str], pattern: re.Pattern) -> str | None:
    return next((f for f in files if pattern.search(f)), None)


def _fallback_dir(root: Path, names: tuple[str, ...]) -> str:
    for name in names:
        if (root / name).is_dir():
            return name
    return "."


def plant_node(root: Path, files: list[str]) -> tuple[str, str] | None:
    existing = _first(files, NODE_TEST_RE)
    if existing:
        match = NODE_TEST_RE.search(existing)
        rel = str(Path(existing).parent / f"zz-verify-break.{match.group(1)}{match.group(2)}")
    else:
        rel = str(Path(_fallback_dir(root, ("test", "tests", "__tests__"))) / "zz-verify-break.test.js")
    body = (f"// Planted by prove_verify.py and removed automatically.\n"
            f'test("planted break", () => {{ throw new Error("{MARK}"); }});\n')
    return rel, body


def plant_python(root: Path, files: list[str]) -> tuple[str, str] | None:
    existing = _first(files, PY_TEST_RE)
    folder = str(Path(existing).parent) if existing else _fallback_dir(root, ("tests", "test"))
    body = ("# Planted by prove_verify.py and removed automatically.\n"
            "import unittest\n\n\n"
            "class PlantedBreak(unittest.TestCase):\n"
            "    def test_planted_break(self):\n"
            f'        self.fail("{MARK}")\n')
    return str(Path(folder) / "test_zz_verify_break.py"), body


def plant_go(root: Path, files: list[str]) -> tuple[str, str] | None:
    source = _first(files, GO_TEST_RE) or next((f for f in files if f.endswith(".go")), None)
    if not source:
        return None
    match = re.search(r"(?m)^package\s+(\w+)", verify_stack.read_text(root / source))
    if not match:
        return None
    pkg = match.group(1)
    body = (f"// Planted by prove_verify.py and removed automatically.\npackage {pkg}\n\n"
            f'import "testing"\n\nfunc TestPlantedBreak(t *testing.T) {{ t.Fatal("{MARK}") }}\n')
    return str(Path(source).parent / "zz_verify_break_test.go"), body


def plant_dotnet(root: Path, files: list[str]) -> tuple[str, str] | None:
    for proj in (f for f in files if f.endswith(".csproj")):
        text = verify_stack.read_text(root / proj)
        lowered = text.lower()
        if "xunit" in lowered:
            cls_attr, method_attr = "", "[Xunit.Fact]"
        elif "nunit" in lowered:
            cls_attr, method_attr = "", "[NUnit.Framework.Test]"
        elif "mstest" in lowered:
            cls_attr = "[Microsoft.VisualStudio.TestTools.UnitTesting.TestClass]\n"
            method_attr = "[Microsoft.VisualStudio.TestTools.UnitTesting.TestMethod]"
        else:
            continue
        body = (f"// Planted by prove_verify.py and removed automatically.\n{cls_attr}"
                f"public class ZzVerifyBreakTest\n{{\n    {method_attr}\n"
                f'    public void PlantedBreak() => throw new System.Exception("{MARK}");\n}}\n')
        return str(Path(proj).parent / "ZzVerifyBreakTest.cs"), body
    return None


PLANTERS = {"node": plant_node, "python": plant_python, "go": plant_go, "dotnet": plant_dotnet}


def auto_break(root: Path) -> tuple[str, str, str] | None:
    files = list(verify_stack.walk_files(root))
    for stack in verify_stack.detect_stacks(root):
        planted = PLANTERS[stack](root, files)
        if planted:
            rel, body = planted
            return os.path.normpath(rel), body, stack
    return None


# ---------------------------------------------------------------- running

def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def git_status(root: Path) -> str:
    return git(root, "status", "--porcelain", "--untracked-files=all").stdout


def run_verify(root: Path, cmd: str, timeout: float) -> dict:
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(root), capture_output=True, text=True, timeout=timeout)
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        code, output = 124, f"timeout after {timeout}s\n{partial}"
    tail = "\n".join(output.rstrip().splitlines()[-15:])
    return {"exit_code": code, "passed": code == 0, "seconds": round(time.monotonic() - start, 2), "tail": tail}


class Break:
    """Apply a break to one file and undo it exactly."""

    def __init__(self, root: Path, rel: str, text: str, append: bool):
        # append=True (user break): modify or create rel. append=False (planted): rel must not exist.
        self.root = root
        self.path = (root / rel).resolve()
        if root.resolve() not in self.path.parents:
            raise UsageError(f"break file is outside the repo: {rel}")
        if self.path.exists() and not append:
            raise UsageError(f"refusing to overwrite existing file: {self.path}")
        if self.path.is_dir():
            raise UsageError(f"break file is a directory: {rel}")
        self.text = text
        self.original: bytes | None = None
        self.created_dirs: list[Path] = []
        self.cache_existed = (self.path.parent / "__pycache__").is_dir()
        self.applied = False

    def apply(self) -> None:
        if self.path.exists():
            self.original = self.path.read_bytes()
        else:
            parent = self.path.parent
            missing = []
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for d in reversed(missing):
                d.mkdir()
                self.created_dirs.append(d)
        self.applied = True
        with open(self.path, "a" if self.original is not None else "w", encoding="utf-8") as fh:
            if self.original is not None and not self.original.endswith(b"\n"):
                fh.write("\n")
            fh.write(self.text)

    def revert(self) -> None:
        if not self.applied:
            return
        if self.original is not None:
            self.path.write_bytes(self.original)
        elif self.path.exists():
            self.path.unlink()
        cache = self.path.parent / "__pycache__"
        if self.path.suffix == ".py" and cache.is_dir():
            if self.original is None:
                for pyc in cache.glob(f"{self.path.stem}.*.pyc"):
                    pyc.unlink()
            if not self.cache_existed and not any(cache.iterdir()):
                cache.rmdir()
        for d in reversed(self.created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass


def prove(root: Path, cmd: str, break_file: str | None = None, break_text: str | None = None,
          timeout: float = 1800) -> dict:
    if break_file:
        rel, body, source = break_file, break_text or "", "user"
    else:
        found = auto_break(root)
        if not found:
            raise UsageError("cannot plant a break for this stack; pass --break-file and --break-patch")
        rel, body, source = found
    result = {"root": str(root), "verify_command": cmd, "break": {"file": rel, "source": source},
              "runs": {}, "clean_after_revert": None, "proven": False}
    brk = Break(root, rel, body, append=bool(break_file))  # validates the path before any run

    first = run_verify(root, cmd, timeout)
    result["runs"]["clean"] = {**first, "expected": "pass"}
    if not first["passed"]:
        result["reason"] = "verify fails on the clean tree; fix it before proving it"
        return result

    status_before = git_status(root)
    try:
        brk.apply()
        second = run_verify(root, cmd, timeout)
    finally:
        brk.revert()
    result["runs"]["broken"] = {**second, "expected": "fail"}
    status_after = git_status(root)
    result["clean_after_revert"] = status_after == status_before
    if not result["clean_after_revert"]:
        result["status_diff"] = {"before": status_before.splitlines(), "after": status_after.splitlines()}

    third = run_verify(root, cmd, timeout)
    result["runs"]["reverted"] = {**third, "expected": "pass"}

    if second["passed"]:
        result["reason"] = "verify still passes with the break planted; it does not run that check"
    elif not result["clean_after_revert"]:
        result["reason"] = "git status differs after revert; inspect the working tree"
    elif not third["passed"]:
        result["reason"] = "verify fails after revert; it is flaky or the break left state behind"
    else:
        result["proven"] = True
        result["reason"] = "verify passes clean, fails on a planted break, passes after revert"
    return result


def human(result: dict) -> str:
    lines = [f"verify: {result['verify_command']}",
             f"break:  {result['break']['file']} ({result['break']['source']})"]
    for label, run in result["runs"].items():
        ok = run["passed"] == (run["expected"] == "pass")
        got = "pass" if run["passed"] else "fail"
        lines.append(f"  {label:<9} expected {run['expected']}, got {got} ({run['seconds']}s) "
                     f"{'ok' if ok else 'WRONG'}")
        if not ok and run["tail"]:
            lines += ["    " + line for line in run["tail"].splitlines()]
    if result["clean_after_revert"] is not None:
        lines.append(f"  git status after revert: {'clean' if result['clean_after_revert'] else 'CHANGED'}")
    lines.append(("PROVEN: " if result["proven"] else "NOT PROVEN: ") + result["reason"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove verify passes clean, fails on a planted break, "
                                                 "and passes again after revert.")
    parser.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    parser.add_argument("--cmd", help="verify command (default: detect package.json verify, scripts/verify.sh, "
                                      "verify.sh, or make verify)")
    parser.add_argument("--break-file", help="file to create or append to as the break (repo-relative)")
    parser.add_argument("--break-patch", help="text to append to --break-file; prefix with @ to read it from a file")
    parser.add_argument("--timeout", type=float, default=1800, help="timeout per verify run, seconds")
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    top = git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print("error: not a git repository (needed to check the revert is clean)", file=sys.stderr)
        return 2
    if bool(args.break_file) != bool(args.break_patch):
        print("error: --break-file and --break-patch go together", file=sys.stderr)
        return 2
    break_text = args.break_patch
    if break_text and break_text.startswith("@"):
        try:
            break_text = Path(break_text[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read {break_text[1:]}: {exc}", file=sys.stderr)
            return 2
    cmd = args.cmd or verify_stack.existing_verify(root)
    if not cmd:
        print("error: no verify command found; run make_verify.py --write or pass --cmd", file=sys.stderr)
        return 2
    try:
        result = prove(root, cmd, args.break_file, break_text, args.timeout)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else human(result))
    return 0 if result["proven"] else 1


if __name__ == "__main__":
    sys.exit(main())
