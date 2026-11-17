---
name: set-up-work-triggers
description: Use when a repo needs a safe way to start an agent or automation run from GitHub - a label added to an issue, a comment command like /run, or a cron schedule. Generates a GitHub Actions workflow with minimal permissions, a per-issue concurrency group, a running-label guard against double starts, and no script injection from issue or comment text.
---

# Set up work triggers

Generate one GitHub Actions workflow that starts a run when work is handed over. The script writes the YAML; you decide which triggers, what the run does, and what permissions it really needs.

## When to use

- The team wants "add label X" or "comment /run" to start a run on an issue.
- Runs should also start on a schedule (nightly queue drain, weekly sweep).
- An existing trigger workflow interpolates issue or comment text into shell. Lint it, then replace it.

## Steps

1. Pick the triggers. Fewer is better. A label is easy to audit. A comment command is easy to use but needs the author check (built in). A schedule has no issue context.
2. Pick the run command. Point it at a script in the repo (`./scripts/agent-run.sh`), not a long inline command. The script reads context from env vars: `EVENT_NAME`, `ISSUE_NUMBER`, `ISSUE_TITLE`, `COMMENT_BODY`. Treat `ISSUE_TITLE` and `COMMENT_BODY` as untrusted data: quote them, never `eval` them.
3. Dry run and read the YAML:

   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/make_trigger_workflow.py" \
     --label agent:run --command /run --cron "0 6 * * 1" \
     --run-command './scripts/agent-run.sh'
   ```

4. Write it:

   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/make_trigger_workflow.py" \
     --label agent:run --command /run --run-command './scripts/agent-run.sh' --write
   ```

5. Widen permissions only as far as the run needs. The default job has `contents: read` plus `issues: write` (and `pull-requests: write` with a comment command) for the guard label. If the run pushes branches or opens PRs, add `contents: write` / `pull-requests: write` by hand, and set `persist-credentials: true` on checkout or pass a token. Say why in the PR.
6. Lint any workflow you edit by hand:

   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/make_trigger_workflow.py" --lint .github/workflows/agent-run.yml
   ```

## Options

| Flag | Meaning |
|---|---|
| `--name` | Workflow name and file name, kebab-case. Default `agent-run`. |
| `--label` | Start when this label is added to an issue. |
| `--command` | Start on a comment that is exactly this command, or starts with it plus a space. OWNER, MEMBER, COLLABORATOR only. |
| `--cron` | Start on a schedule (UTC). Repeatable. |
| `--run-command` | One-line command for the run step. Passed via `env`. No `${{ }}` allowed. |
| `--running-label` | Label that marks a run in progress. Default `running`. |
| `--timeout-minutes` | Job timeout. Default 60. |
| `--repo` | Repo root to write into. Default current dir. |
| `--write` / `--force` | Write the file / overwrite an existing one. Default is dry run. |
| `--lint PATH` | Only lint an existing workflow for script injection. |
| `--json` | Machine output: path, written, triggers, problems, yaml. |

Exit codes: 0 ok, 1 lint problems, 2 usage error (bad flag, file exists without `--force`).

## What the generated workflow guarantees

- **No script injection.** Event fields an outsider controls (comment body, issue title and body, label name, PR head ref) appear only in `env:` values and `if:` expressions, never inside `run:` scripts. The lint enforces this.
- **No double starts.** A concurrency group per issue number (`cancel-in-progress: false`) queues a second trigger behind the first. The guard step skips the run if the running label is already on the issue, otherwise adds it. The release step removes it under `always()`, so a failed or cancelled run does not leave the issue locked.
- **Trusted authors only** for comment commands, via `author_association`.
- **Minimal permissions.** `permissions: {}` at the top; the job asks only for what the guard needs.

## Judgment calls

- A comment on a pull request runs against the default branch checkout, not the PR head. If the run must build PR code, check out the head ref in the run script and treat that code as untrusted.
- Do not switch to `pull_request_target` to get label triggers on PRs. It runs with write tokens against fork code.
- If the running label gets stuck (runner died before release), remove it by hand. Do not add auto-expiry without a reason.
- Pin `actions/checkout` to a commit SHA if the repo pins actions.
- Keep the schedule coarse. A run that finds nothing to do should exit fast.
