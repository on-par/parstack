---
name: assess-agent-readiness
description: Score a repository 0-100 on how safely agents can work in it and get a ranked fix list. Use before pointing agents or an automated loop at a codebase, when onboarding a new repo, or to re-check readiness after fixes. Checks for a single verify command, verify speed and flakiness, module boundary rules, dead-code and duplication tooling, secrets, agent context files, CI, and tests. TypeScript and .NET first; other stacks get the generic checks.
---

# Assess agent readiness

Agents are only as safe as the repo's feedback loop. This skill measures that loop and tells you what to fix first.

## Run

```
python3 "${CLAUDE_SKILL_DIR}/scripts/assess.py" [path]
python3 "${CLAUDE_SKILL_DIR}/scripts/assess.py" [path] --json
```

Options:
- `--runs N` runs the verify command N times to time it and detect flakiness. Default 0 (does not run anything). Use 3 or more once you know verify is safe to run locally.
- `--verify-cmd "..."` overrides the detected verify command.
- `--slow-seconds S` warns when median verify time is above S (default 600).
- `--min-score N` exits 1 when the score is below N. Default 0, so it never blocks unless asked.

Exit codes: 0 at or above `--min-score`, 1 below it, 2 usage error.

## Checks

| id | weight | pass when |
|---|---|---|
| verify_command | 20 | package.json `verify`/`check`, Makefile or justfile `verify`, `verify.sh`, `scripts/verify.sh`, or a .NET solution |
| verify_runs | 10 | all `--runs` pass, median under `--slow-seconds` (skipped by default) |
| boundaries | 15 | TS: dependency-cruiser or madge config. .NET: ArchUnitNET or NetArchTest referenced |
| secrets | 15 | gitleaks finds nothing; without gitleaks a regex scan of tracked files runs and caps at warn |
| dead_code | 10 | TS: knip or ts-prune. .NET: analyzer settings or unused-code diagnostics |
| agent_context | 10 | AGENTS.md, CLAUDE.md, .cursor/rules, .cursorrules, or copilot-instructions |
| ci | 10 | a CI config file exists |
| duplication | 5 | jscpd configured or installed as a dev dependency |
| tests | 5 | test dirs or test files exist |

Status factors: pass 1, warn 0.5, fail 0. Skipped checks drop out of the denominator. The score is the weighted share of points earned.

Files come from `git ls-files --cached --others --exclude-standard`, so ignored files are never scanned. Without git, the script walks the tree and skips build and dependency dirs.

## How to use the result

1. Run the script and read the ranked fix list. Rank is by points lost, highest first.
2. Fix in rank order. A missing verify command comes before anything else: no other check matters if an agent cannot tell pass from fail.
3. Once verify exists, run with `--runs 3`. A flaky verify sends agents chasing ghosts, so fix flakiness before you add agents.
4. Re-run and record the score in the PR that improves it.

## Judgment

- A regex secret hit can be a false positive (test fixtures, example values). Read each hit before you act. A real one gets rotated, not just deleted.
- A .NET solution counts as a verify command because `dotnet test` builds and tests in one step. If the repo also needs format or lint checks, add a real verify script anyway.
- "unknown stack" means boundaries and dead-code checks are skipped, not passed. Say so in your summary.
- Do not chase 100. Above about 80, the remaining points are usually cheap tooling, not safety.

Related skills: add-one-verify-command, enforce-module-boundaries, shrink-quality-budgets.
