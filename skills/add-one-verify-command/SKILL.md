---
name: add-one-verify-command
description: Give a repo one verify command that chains its build, lint and test steps, then prove it fails on a planted break and passes again after revert. Use when a repo has no single pass/fail command for agents or CI, when the verify command is suspected of not running the tests, or after changing what verify runs. Supports Node/TypeScript (npm, pnpm, yarn, bun), .NET, Python and Go.
---

# Add one verify command

An agent needs one command that answers "is this change good?". Exit 0 means yes. Anything else means no. This skill writes that command and proves it can actually say no.

## 1. Propose and write

```
python3 "${CLAUDE_SKILL_DIR}/scripts/make_verify.py" [path]            # dry run
python3 "${CLAUDE_SKILL_DIR}/scripts/make_verify.py" [path] --write
```

The script detects the stack and chains the steps that already exist:

- Node/TS (package.json; the lockfile picks npm, pnpm, yarn or bun): the `lint`, `typecheck`, `build` and `test` scripts, cheapest first. It writes a package.json `"verify"` script and keeps the file's indent.
- .NET (`*.sln` or a single `*.csproj` at root): `dotnet format --verify-no-changes`, `dotnet build`, `dotnet test --no-build`.
- Python (pyproject/setup): ruff or flake8 if configured, mypy if configured, then pytest or unittest.
- Go (go.mod): `go build`, `go vet`, golangci-lint if configured, `go test`.

Every stack except node-only gets `scripts/verify.sh` with `set -euo pipefail`. Mixed stacks also get `verify.sh`, and it runs every detected stack. Use `--stack <name>` (repeatable) to limit the stacks.

An existing verify entry point is left alone unless you pass `--force`. Exit codes: 0 proposed, written or already present; 1 nothing to chain; 2 usage error. `--json` prints the steps and the content.

## 2. Edit with judgment

Read the dry run before you write. Decide:

- **What belongs.** Verify gates every change, so it holds checks that must pass to merge: build, type check, lint, unit tests. Leave out deploys, publishing, and anything that needs credentials or network.
- **Speed.** Agents run verify many times per task. Aim for under a few minutes. Put the cheapest steps first so a lint failure costs seconds. Move slow end-to-end suites to CI and say so in AGENTS.md.
- **No auto-fix in verify.** Formatters run in check mode (`--check`, `--verify-no-changes`). A verify that rewrites files hides failures.
- **Missing steps.** If the repo has no lint or no tests, the proposal will be short. Do not invent steps. Record the gap and add the tool in a separate change.

Hand-edit the generated script or package.json entry if needed. Run it once yourself.

## 3. Prove it

```
python3 "${CLAUDE_SKILL_DIR}/scripts/prove_verify.py" [path]
python3 "${CLAUDE_SKILL_DIR}/scripts/prove_verify.py" [path] --cmd "make verify"
python3 "${CLAUDE_SKILL_DIR}/scripts/prove_verify.py" [path] --break-file src/math.ts --break-patch @break.txt
```

Steps:
1. Run verify on the current tree. It must pass. If it fails, the script stops and plants nothing.
2. Plant a break and run verify. It must fail. By default the break is a known-failing test placed next to an existing test file (`zz-verify-break.test.*`, `test_zz_verify_break.py`, `zz_verify_break_test.go`, or `ZzVerifyBreakTest.cs` in the first xunit, nunit or mstest project). With `--break-file` and `--break-patch`, the patch text is appended to that file. If the file does not exist, it is created. Prefix the patch with `@` to read it from a file.
3. Revert the break in a `finally` block and compare `git status` with its state before the break.
4. Run verify again. It must pass.

Requires a git repo. The verify command is detected from package.json `verify`, `scripts/verify.sh`, `verify.sh`, or a Makefile `verify` target. Pass `--cmd` to override. Exit codes: 0 proven, 1 not proven (the reason is printed), 2 usage error.

The default break proves that verify runs tests. If you need proof that verify runs lint or the type check, use `--break-file` with a lint or type error.

## Done when

- One command, documented in AGENTS.md (or README) as the thing to run before every commit.
- `prove_verify.py` prints PROVEN.
- CI runs the same command.
