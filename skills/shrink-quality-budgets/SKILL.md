---
name: shrink-quality-budgets
description: Hold code-quality metrics (lines of code, comment lines, duplication, dead code, boundary violations) under budgets that only go down. Use when adding a quality gate to verify or CI, when a budget check fails after a change, or after a cleanup to lock in the lower numbers. Missing tools are skipped, not failed.
---

# Shrink quality budgets

Agents add code fast and rarely delete it. A budget caps each metric at today's value. A change must keep every metric at or under its budget. A cleanup lowers the budget so the gain sticks.

## Budgets file

`quality-budgets.json` at the repo root (`--config` to move it):

```json
{
  "include": ["src/**"],
  "exclude": ["**/*.test.ts", "**/generated/**"],
  "budgets": { "loc": 12000, "comment_lines": 900, "duplication": 3.5, "dead_code": 12, "boundary_violations": 4 },
  "sources": { "jscpd_report": "", "knip_report": "", "depcruise_report": "", "boundaries_baseline": "boundaries-baseline.json" }
}
```

Every budget is an upper bound. Leave out a metric to stop tracking it. Glob rules: `**` crosses directories, and a pattern without `/` matches a file name at any depth. Empty `include` means all files.

## Metrics

| metric | how it is measured | skipped when |
|---|---|---|
| loc | non-blank, non-comment lines in .ts .tsx .js .jsx .mjs .cjs .cs .py .go .java | never |
| comment_lines | lines that hold only a comment (`//`, `/* */`, `#`). Python docstrings count as code | never |
| duplication | jscpd total duplicated-lines %, from `sources.jscpd_report` or a jscpd run | no report and no jscpd |
| dead_code | knip issue count (unused files, exports, types, deps), from `sources.knip_report` or a knip run | no report and no knip |
| boundary_violations | dependency-cruiser violations from `sources.depcruise_report` or a depcruise run (needs a config), else `count` in the boundaries baseline | none of those |

Files come from `git ls-files --cached --others --exclude-standard`, so `.gitignore` is respected. Without git the script walks the tree and skips build and dependency dirs. Tools are looked up in `node_modules/.bin`, then `PATH`.

## Commands

```
python3 "${CLAUDE_SKILL_DIR}/scripts/budgets.py" init               # write budgets at current values
python3 "${CLAUDE_SKILL_DIR}/scripts/budgets.py" measure            # print current values
python3 "${CLAUDE_SKILL_DIR}/scripts/budgets.py" check              # fail if any metric is over budget
python3 "${CLAUDE_SKILL_DIR}/scripts/budgets.py" check --tighten    # also lower budgets to current values
```

Common flags: `--root DIR`, `--config FILE`, `--json`. `init` refuses to overwrite an existing file without `--force`.

Exit codes: 0 all metrics within budget (skipped metrics do not count), 1 at least one metric over budget, 2 usage error (missing or invalid budgets file).

## Workflow

1. Run `init` once. Set `include` and `exclude` so the counts cover real source, not fixtures, vendored or generated code. Re-run `init --force` after changing them.
2. Add `budgets.py check` to verify or CI.
3. When a change makes a metric go down, run `check --tighten` and commit the lower budget with the change.
4. When `check` fails, fix the code: delete the dead export, extract the duplicate, fix the boundary violation. Comment lines are budgeted to stop narration comments from piling up. Keep comments that explain why, and delete ones that restate the code.

## Raising a budget

`--tighten` never raises a budget. Raising one is a deliberate manual edit to `quality-budgets.json`, in its own commit or clearly called out in the PR, with the reason. Legitimate reasons: a feature that genuinely needs the code, or a newly tracked area added to `include`. "The check failed" is not a reason.

## Judgment

- A skipped metric is unknown, not passing. Report skipped metrics in your summary so a human can decide whether to install the tool.
- LOC growth from a real feature is expected. The budget makes the growth visible and deliberate. It is not meant to forbid it.
- Prefer one cleanup PR that tightens several budgets over many tiny edits.

Related: enforce-module-boundaries writes the baseline whose `count` feeds `boundary_violations`.
