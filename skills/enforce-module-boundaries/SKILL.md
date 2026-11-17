---
name: enforce-module-boundaries
description: Generate and enforce module boundary rules (no import cycles, no reaching into another module's internals) with a baseline so existing violations do not block work but new ones fail. Use when a codebase has feature or domain folders that agents keep coupling together, when adding boundary checks to verify or CI, or when a boundary check fails and you need to know if the violation is new. TypeScript/JavaScript via dependency-cruiser, .NET via an ArchUnitNET test.
---

# Enforce module boundaries

Agents take the shortest import path. Without rules, every module ends up depending on every other one. This skill turns the folder layout into rules, accepts today's violations as a baseline, and fails only on new ones.

## 1. Pick the modules

Look at the layout before you generate anything. A module is a folder that owns a feature or domain and could be described in one sentence (`src/billing`, `src/features/cart`). Shared helpers (`src/lib`, `src/utils`), test folders and generated code are not modules.

## 2. Generate rules

```
python3 "${CLAUDE_SKILL_DIR}/scripts/gen_boundaries.py" [repo]                               # dry run
python3 "${CLAUDE_SKILL_DIR}/scripts/gen_boundaries.py" [repo] --root src --modules-glob "features/*" --write
python3 "${CLAUDE_SKILL_DIR}/scripts/gen_boundaries.py" [repo] --module src/billing --module src/users --write
```

- `--root` is the folder that holds the modules (default `src` if present). `--modules-glob` matches module dirs under it (default `*`). `--module` (repeatable) lists the modules explicitly and overrides the glob. `--exclude NAME` skips more dir names. Test, asset, `bin` and `obj` dirs are skipped by default.
- TS/JS writes `.dependency-cruiser.cjs` with two rules:
  - `no-circular`: no import cycles anywhere.
  - `no-cross-module-internals`: a file in one module may import another module only through its `index.(ts|tsx|js|jsx|mjs|cjs)`.
  - Path aliases (`@/x`) resolve through the closest `tsconfig.json` from `--root` up to the repo root, set as `options.tsConfig`; check it is the tsconfig that defines `paths`. The check warns when alias imports stay unresolved.
- .NET writes `ModuleBoundariesTests.cs` (ArchUnitNET + xUnit) into the first test project it finds, or the path given with `--out`. Each module namespace must not depend on any other module namespace. Folder modules get the namespace `<RootNamespace>.<Folder>`; set `--namespace-prefix` to override it. A module dir with its own csproj uses that project's namespace. The test project needs `TngTech.ArchUnitNET.xUnit` and references to the module assemblies.
- An existing file is not overwritten without `--force`. Exit codes: 0 ok, 1 fewer than two modules found, 2 usage error.

## 3. Baseline, then check

```
python3 "${CLAUDE_SKILL_DIR}/scripts/check_boundaries.py" [repo] --update-baseline   # once, to accept today's state
python3 "${CLAUDE_SKILL_DIR}/scripts/check_boundaries.py" [repo]                     # in verify or CI
```

- TS: runs dependency-cruiser from `node_modules/.bin`, `PATH`, or `npx --no-install`, cruising `src` (or `--source`). `--depcruise-json FILE` reads a saved report instead.
- .NET: runs `dotnet test --filter FullyQualifiedName~ModuleBoundariesTests` and reads the failed module pairs from the output. `--dotnet-output FILE` reads saved output instead.
- Violations are keyed by rule, from and to, and stored in `boundaries-baseline.json` (or `--baseline`) with a `count`. shrink-quality-budgets reads that count.
- Fails only on violations missing from the baseline. Fixed violations are reported with a hint to run `--update-baseline`, so the baseline only shrinks.
- If the tool is missing, the result is `skipped` with exit 0. A tool crash or a test build failure is `error` with exit 1.

Exit codes: 0 no new violations, skipped, or baseline updated; 1 new violations or error; 2 usage error.

## Judgment

- **New violation.** The default fix is the code, not the baseline: import through the other module's index, move the shared code into a shared folder, or invert the dependency.
- **Legit exception.** Add it to the rule itself with a one-line reason: a `pathNot` entry in the depcruise rule, or an `Allowed` entry in the C# test. Do not hide it in the baseline. The baseline is for debt you intend to pay down.
- **When to run `--update-baseline`.** Run it after fixing violations, to lock in the lower count. Run it on first adoption. Never run it to make a failing check pass. Changing the baseline is a reviewed change.
- **Layout drift.** When modules are added or renamed, regenerate the rules and diff them against the committed file before you overwrite it.

Related: shrink-quality-budgets tracks `boundary_violations` from the baseline count.
