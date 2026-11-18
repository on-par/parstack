---
name: check-issue-readiness
description: Lint a single issue (GitHub, a markdown file, or an exported Linear/Jira-style JSON) before anyone starts it, and get a verdict of ready, ready-with-assumptions, or not-ready with the exact list of what is missing. Use before picking up an issue, before handing an issue to an agent loop, or when grooming a backlog to find issues that need acceptance criteria, proof, scope, dependencies, or slicing.
---

# Check issue readiness

An issue is ready when someone with no extra context can finish it and prove it is done. This skill checks that mechanically, then you fill the gaps.

## What ready means

- **Acceptance criteria:** a list of observable outcomes, under a heading like "Acceptance criteria", "Done when", or as a checkbox list.
- **Proof per criterion:** each criterion names a test, check, or command on a `proof:` / `test:` / `cmd:` line, inline or as a child bullet (`proof: test_rejects_expired_token`, `` proof: `npm test -- reset` ``). "Manual" is not proof. A backticked word in the criterion text itself (`` `syncMode: strict` is honored ``) is the outcome, not its proof.
- **Scope and out of scope:** what changes, and what explicitly does not.
- **Dependencies:** "Depends on #N", "Blocked by #N", "Requires #N", a bare `#N` bullet in a Dependencies section, or "Dependencies: None". Other refs ("builds on #N", "Related: #N", "after #N") are not dependencies. Put related issues in a separate `## Related` section; it is never read as dependencies.
- **Small:** at most 5 criteria and at most 5 scope items. Bigger issues get sliced.
- **Resolved:** no TBD, TODO, `??`, or open questions left in the text, and no `blocked` / `needs-info` / `question` label.
- **Precise:** criteria avoid vague words (fast, properly, intuitive, robust, better, etc).

## Run

```
python3 "${CLAUDE_SKILL_DIR}/scripts/check_issue.py" --gh 123 [--repo owner/name]
python3 "${CLAUDE_SKILL_DIR}/scripts/check_issue.py" --file issue.md
python3 "${CLAUDE_SKILL_DIR}/scripts/check_issue.py" --json-file issue.json
```

Add `--json` for machine output. `--strict` makes ready-with-assumptions exit 1 (use in unattended loops that must not guess). `--max-items N` changes the size limit (default 5).

- `--gh` needs the `gh` CLI. If it is missing the script prints `skipped: gh not found` and exits 2. Fall back to `gh`-free input: paste the issue into a markdown file and use `--file`.
- `--file`: the first `# heading` is the title. `gh issue create --body-file` posts the whole file, title line included. Use `--emit-body` to print the body without the title line (the verdict goes to stderr, and the exit code is the usual one):
  `check_issue.py --file issue.md --emit-body > body.md && gh issue create --title "..." --body-file body.md`
- Issues rendered by `slice-to-smallest-value` name dependencies as slice ids (`Depends on S1`). Those are reported as unresolved dependencies. To file a slice set, use its `file_slices.py --repo owner/name` (dry run by default; `--create` posts). It files slices in dependency order, rewrites slice ids to the new issue numbers, runs this check on each body before posting and refuses a not-ready one, then re-checks each new issue with `--gh N`.
- `--json-file`: accepts `title`/`body` (GitHub), `title`/`description` (Linear), or `fields.summary`/`fields.description` (Jira, including rich-text documents).

Exit codes: 0 ready or ready-with-assumptions, 1 not-ready, 2 input error or skipped.

## Verdicts

- **not-ready** (blocking, listed under `missing`): no acceptance criteria; more than 5 criteria or scope items; no proof named for any criterion and no Testing section; unresolved TBD/TODO/`??`; open questions; a blocking label.
- **ready-with-assumptions** (listed under `assumptions`): some criteria lack proof; no scope or out-of-scope section; no dependency statement; a dependency that is not an issue reference (`S1`); vague wording in a criterion. The script states the assumption it would make for each.
- **ready**: nothing missing, nothing assumed.

`notes` lists dependencies to confirm are merged before starting. They do not change the verdict.

An `## Assumptions` section is read and counted (`assumptions_documented` in `--json`, "N documented assumptions" in text). A documented assumption about dependencies replaces the automatic "no dependencies stated" one.

## What to do with the result

1. **not-ready, too big:** slice it. Split into issues of at most 5 criteria each, each a vertical slice of user-visible value, with explicit dependencies between them.
2. **not-ready, missing criteria or proof:** draft the missing criteria and a proof for each from the issue text and the code. Present them to the issue owner; do not invent product decisions.
3. **not-ready, open questions or TBD:** list the questions. Ask the owner. Do not start.
4. **ready-with-assumptions:** read each assumption. If it is safe, write it into the issue (an `## Assumptions` section) so the next reader sees it, then start. If it is not safe, ask.
5. **ready:** confirm the `notes` dependencies are closed, then start.

Editing or commenting on a remote issue is outward-facing. Show the proposed text and confirm before posting it.

## Example issue that passes

```markdown
# Expire password reset links after one hour

## Scope
- Store issued-at on the reset token
- Reject tokens older than 60 minutes

## Out of scope
- Changing the email template

## Acceptance criteria
- [ ] A 61-minute-old token is rejected with "link expired"
  - proof: test_expired_token_rejected
- [ ] A 59-minute-old token still works
  - proof: `npm test -- reset`

## Dependencies
- Depends on #41

## Related
- #38
```

## Judgment that stays with you

- Whether a criterion is really observable, even if the script found a proof for it.
- Whether an assumption is safe to make or needs the owner.
- Whether vague wording matters in context ("fast" with a stated p95 budget is fine).
- The script reads structure, not meaning. A ready verdict on a wrong issue is still wrong.
