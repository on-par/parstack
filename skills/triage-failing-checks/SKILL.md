---
name: triage-failing-checks
description: Use when a check, test suite, or CI job fails on a branch and you need to know whether this change caused it before trying a fix. Runs the check on HEAD and on the merge-base in a temporary worktree, labels each failure regression, already-broken, or flaky, and counts rework rounds so a stuck loop stops after 3 attempts and escalates to a human.
---

# Triage failing checks

Before fixing a red check, find out whose failure it is. The script runs the check on your branch and on the commit it branched from, then labels each failure. You decide what to fix, what to report, and when to stop.

## Labels

| Label | Head | Base | What to do |
|---|---|---|---|
| `regression` | fails every run | passes | Your change broke it. Fix it in this change. |
| `already-broken` | fails every run | fails | Not caused by this change. Report it; do not fix it here. |
| `flaky` | mixed across `--repeat` runs | any | Find out if this change made it flaky (compare against base with `--repeat` on a base checkout). Do not retry until green. |
| `unclear` | fails every run | base run crashed or produced no parseable results | Fix the base run (often missing `--setup`), then rerun. |
| `fixed` | passes | fails | Informational. |

Exit 1 when any `regression` or `flaky` is found, or when the round limit is reached. Exit 0 when clean or only `already-broken`/`unclear`. Exit 2 on usage errors.

## Steps

1. Pick the check command. Use the same command CI runs, or the narrowest command that reproduces the failure.
2. Run triage:

   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/triage_checks.py" --cmd "npm test" --repeat 3 --setup "npm ci"
   ```

3. Read the labels. Fix only regressions, and flakiness this change introduced. One fix attempt per round.
4. Rerun the same command after each fix. Each run with failures on head counts as one rework round. A run with no failures on head clears the count.
5. When the script returns `status: escalate`, stop. Do not attempt another fix. Hand the last report to a human with what you tried in each round.

## Per-test results

Without a parser, the whole command is one check named `(command)`. To label individual tests:

- JUnit XML: `--parse junit:reports/junit.xml` (a file or a directory of XML files, relative to where the command runs). Reports older than the run are ignored.
- Regex over output: `--parse 'regex:^FAIL (?P<id>\S+)'` (named group `id`, or the first group).
- Known failing ids: `--ids test_a test_b` (or `--ids-file`). Put `{id}` in `--cmd` to run each id on its own (`--cmd "pytest {id}"`); ids are shell-quoted. Without `{id}`, give `--parse` and results are filtered to those ids.

If the command fails and the parser finds nothing, the run is marked as having no parseable results and counted as a `(command)` failure. That usually means a build error or the wrong parser.

## Options

| Flag | Meaning |
|---|---|
| `--cmd` | Check command, run through the shell. Required. |
| `--base` | Ref to compare against. Default: merge-base of HEAD with `origin/main`, then `origin/master`, `main`, `master`. |
| `--repeat N` | Runs on head, to detect flakiness. Default 1. Use 3 or more when a failure looks intermittent. |
| `--setup` | Command run in the base worktree before the check (install deps, build). |
| `--skip-base` | Flakiness check only. |
| `--timeout` | Seconds per command run. Default 1800. |
| `--repo` | Directory to run in. The base run uses the same subdirectory when it exists. |
| `--state` | Rounds file. Default `<repo root>/.triage-rounds.json`. Add it to `.gitignore`. |
| `--max-rounds` | Rounds before refusing. Default 3. |
| `--reset-rounds` | Clear the count for this branch and command. A human decision, not a way around the limit. |
| `--no-state` | Do not read or write the rounds file. |
| `--json` | Machine output: status, head, base, runs, failures, counts, round, message. |

## How it runs

- Head runs in the current worktree, including uncommitted changes. The report flags a dirty tree.
- Base runs in a temporary `git worktree add --detach` at the merge-base, removed in a `finally` even on error or interrupt. Untracked files (dependencies, build output, `.env`) are not there; use `--setup`.
- The base is skipped when head has no failures.
- Rounds are keyed by branch name and command.

## Judgment calls

- A regression in a test you did not touch is still yours if head fails and base passes. Check what your change affects before calling it noise.
- `already-broken` failures still block merging in most repos. Report them separately (issue or note) instead of mixing an unrelated fix into this change.
- Never mark a flaky test as skipped to get green without saying so in the change description.
- If base cannot be built at all, say so and compare against a known-good commit with `--base <sha>`.
