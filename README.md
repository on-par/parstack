# parstack

Nine agent skills for getting a codebase ready for agent work, managing work going in, and keeping runs healthy. Works with any repo, any agent loop, or a human team. Scripts do the deterministic work; the agent keeps the judgment.

Based on On PAR's factory method.

## Skills

### Get a codebase factory-ready

- `assess-agent-readiness` - score a repo 0-100 on verify command, boundaries, dead code, duplication, secrets, agent context, CI, and tests; print a ranked fix list.
- `add-one-verify-command` - detect the stack and write one `verify` entry point chaining build, lint, and test; prove it catches a planted break.
- `enforce-module-boundaries` - generate cross-module import rules from the folder layout and fail only on new violations against a baseline.
- `shrink-quality-budgets` - measure LOC, comments, duplication, dead code, and boundary violations; fail above budget, tighten to current, never raise.

### Manage work going in

- `slice-to-smallest-value` - break an ask into the smallest end-to-end slices of user-visible value, each with at most 5 scope items and 5 criteria and a named proof per criterion; check coverage, overlap, and dependency order.
- `check-issue-readiness` - lint an issue for acceptance criteria, scope, dependencies, proof, size, and vague words; return ready, ready-with-assumptions, or not-ready.
- `order-the-queue` - build the dependency graph of open issues, flag cycles, missing deps, stale and blocked items, and print run order as parallel groups.
- `set-up-work-triggers` - generate a GitHub Actions workflow that starts a run from a label, a comment command, or a schedule, with double-start guards and minimal permissions.

### Keep runs healthy

- `triage-failing-checks` - run a failing check on head and on the merge-base, label each failure regression, already-broken, or flaky, and stop after 3 rework rounds.

## Install

Claude Code:

```
/plugin marketplace add on-par/parstack
/plugin install parstack@parstack
```

skills.sh:

```
npx skills add on-par/parstack
```

## Requirements

- python3 3.10+
- git

Optional tools, used when present and reported as skipped when missing:

- `assess-agent-readiness`: dependency-cruiser or madge, knip or ts-prune, jscpd, gitleaks, dotnet
- `add-one-verify-command`: the stack's own toolchain (npm, pnpm, yarn, bun, dotnet, python, go)
- `enforce-module-boundaries`: dependency-cruiser (via npx), dotnet
- `shrink-quality-budgets`: jscpd, knip, dependency-cruiser
- `check-issue-readiness`, `order-the-queue`: gh
- `triage-failing-checks`: whatever the check command needs

## Script contract

- Python 3.10+ standard library only. No pip installs.
- Every script takes `--json` for machine output; the default is a short human summary.
- Exit 0 on pass, 1 on blocking findings, 2 on usage or input error.
- Missing optional tools are reported as `skipped: <tool> not found`, never a hard failure.

## Development

```
python3 tools/validate.py
python3 -m unittest discover -s tools -p 'test_*.py'
python3 -m unittest discover -s skills -p 'test_*.py'
```

Each skill is self-contained: no imports from another skill or the repo root. Test dirs need `__init__.py` from the skill dir down so discovery finds them; the validator checks this.

## Evals: golden (every PR)

Golden evals run each skill script against a saved fixture and compare the exit code and output to a saved result. They need no network and no optional tools, and they cost nothing to run.

```
python3 evals/run_golden.py                        # all cases
python3 evals/run_golden.py --skill order-the-queue
python3 evals/run_golden.py --changed-from origin/main
python3 evals/run_golden.py --update               # rewrite expected output
python3 -m unittest discover -s evals -p 'test_*.py'
```

- A case is `evals/golden/<skill>/<case>/` with `case.json` (script, args, cwd, expect_exit, optional git setup steps), a `fixture/` dir, and `expected.json`. If the script does not print JSON, the case uses `expected.txt` instead.
- Each case runs in a temp copy of its fixture. PATH holds only `python3` and `git`, so optional tools always report as not found.
- Before comparing, the runner replaces temp paths, the repo path, git SHAs, timings, and timestamps with fixed values. The rules are in the docstring of `evals/run_golden.py`.
- `--changed-from REF` runs only skills with changes under `skills/<skill>/` or `evals/golden/<skill>/`. If the runner or `.github/` changed, it runs all cases.
- `--update` never changes `expect_exit`, and it does not write a golden when the exit code is wrong. Read every changed `expected.json` before you commit it. A golden that records a bug is worse than no golden.
- CI runs the cases for changed skills on PRs and all cases on push.

## Evals: behavior (opt-in)

Behavior evals run a real agent session per case and check that it picks the right skill and runs its scripts. They cost model calls, so they never run by default.

Each case lives in `evals/behavior/<skill>/<case>/`: a `case.json` (a prompt that reads like a real request and does not name the skill, the expected skill and scripts, an optional artifact check and one-line rubric) and an optional `fixture/` repo. The runner copies the fixture to a temp dir, commits it as a git repo on `main`, runs `claude -p` there with this repo as the plugin dir and a fixed tool allowlist, and grades:

- `skill_triggered`: the Skill tool was called with the skill (plugin-namespaced names count).
- `scripts_invoked`: every expected script appears in a Bash command.
- `artifact_check`: the case's check command exits as expected on the written file.
- `rubric`: a second model call answers PASS or FAIL. Skip with `--no-rubric`.

```
python3 evals/run_behavior.py --skill order-the-queue --skill check-issue-readiness
python3 evals/run_behavior.py --changed-from origin/main --max-budget-usd 0.50 --json
python3 -m unittest discover -s evals -p 'test_run_behavior.py'
```

The default model is `$PARSTACK_EVAL_MODEL` or `haiku`. Without `claude` on PATH the runner prints a notice and exits 0. In CI the `behavior-evals` workflow runs on PRs labeled `run-evals` (changed skills only) or by hand, and skips when no API key secret is set.

## License

MIT. See [LICENSE](LICENSE).
