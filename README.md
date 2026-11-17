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

## License

MIT. See [LICENSE](LICENSE).
