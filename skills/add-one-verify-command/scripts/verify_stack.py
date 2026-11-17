"""Stack detection shared by make_verify.py and prove_verify.py (stdlib only)."""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv", "__pycache__",
             "vendor", "target", "coverage", "fixtures", "testdata"}

NODE_TYPECHECK_NAMES = ("typecheck", "type-check", "types", "tsc")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_package_json(root: Path) -> dict | None:
    path = root / "package.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def walk_files(root: Path, max_depth: int = 6):
    """Yield repo-relative posix paths, skipping dependency/build dirs."""
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            yield (Path(dirpath) / name).relative_to(root).as_posix()


def package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def dotnet_target(root: Path) -> str | None:
    slns = sorted(p.name for p in root.glob("*.sln")) + sorted(p.name for p in root.glob("*.slnx"))
    if slns:
        return slns[0]
    projs = sorted(p.relative_to(root).as_posix() for p in root.glob("*.csproj"))
    if len(projs) == 1:
        return projs[0]
    return None


def detect_stacks(root: Path) -> list[str]:
    stacks = []
    if (root / "package.json").exists():
        stacks.append("node")
    if dotnet_target(root):
        stacks.append("dotnet")
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "setup.cfg").exists():
        stacks.append("python")
    if (root / "go.mod").exists():
        stacks.append("go")
    return stacks


def step(name: str, *argv: str) -> dict:
    return {"name": name, "argv": list(argv), "cmd": shlex.join(argv)}


def node_steps(root: Path) -> list[dict]:
    pkg = read_package_json(root) or {}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    pm = package_manager(root)
    steps = []
    if "lint" in scripts:
        steps.append(step("lint", pm, "run", "lint"))
    for name in NODE_TYPECHECK_NAMES:
        if name in scripts:
            steps.append(step("typecheck", pm, "run", name))
            break
    if "build" in scripts:
        steps.append(step("build", pm, "run", "build"))
    test = scripts.get("test", "")
    if test and "no test specified" not in test:
        steps.append(step("test", pm, "run", "test"))
    return steps


def dotnet_steps(root: Path) -> list[dict]:
    target = dotnet_target(root)
    if not target:
        return []
    return [
        step("lint", "dotnet", "format", target, "--verify-no-changes"),
        step("build", "dotnet", "build", target, "--nologo"),
        step("test", "dotnet", "test", target, "--nologo", "--no-build"),
    ]


def python_steps(root: Path) -> list[dict]:
    config = read_text(root / "pyproject.toml") + read_text(root / "setup.cfg")
    steps = []
    if re.search(r"\bruff\b", config) or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        steps.append(step("lint", "ruff", "check", "."))
    elif re.search(r"\bflake8\b", config) or (root / ".flake8").exists():
        steps.append(step("lint", "python3", "-m", "flake8"))
    if re.search(r"\bmypy\b", config) or (root / "mypy.ini").exists():
        steps.append(step("typecheck", "python3", "-m", "mypy", "."))
    if re.search(r"\bpytest\b", config) or (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        steps.append(step("test", "python3", "-m", "pytest", "-q"))
    elif (root / "tests").is_dir():
        steps.append(step("test", "python3", "-m", "unittest", "discover", "-s", "tests"))
    else:
        steps.append(step("test", "python3", "-m", "unittest", "discover"))
    return steps


def go_steps(root: Path) -> list[dict]:
    steps = [step("build", "go", "build", "./..."), step("lint", "go", "vet", "./...")]
    if any((root / f).exists() for f in (".golangci.yml", ".golangci.yaml", ".golangci.toml", ".golangci.json")):
        steps.append(step("lint", "golangci-lint", "run"))
    steps.append(step("test", "go", "test", "./..."))
    return steps


STEP_FUNCS = {"node": node_steps, "dotnet": dotnet_steps, "python": python_steps, "go": go_steps}


def existing_verify(root: Path) -> str | None:
    """The repo's current verify command, if any."""
    pkg = read_package_json(root)
    if pkg and isinstance(pkg.get("scripts"), dict) and "verify" in pkg["scripts"]:
        return f"{package_manager(root)} run verify"
    for script in ("scripts/verify.sh", "verify.sh"):
        if (root / script).exists():
            return f"bash {script}"
    makefile = root / "Makefile"
    if makefile.exists() and re.search(r"(?m)^verify\s*:", read_text(makefile)):
        return "make verify"
    return None
