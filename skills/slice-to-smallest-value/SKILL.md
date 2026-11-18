---
name: slice-to-smallest-value
description: Break a feature request, epic, or oversized issue into the smallest shippable vertical slices of user-visible value, each with at most 5 scope items, at most 5 acceptance criteria, a named proof per criterion, and explicit dependencies. Use before starting work on anything that would take more than one small change, when an issue is too big to finish and verify in one pass, or when a queue of work needs to run in parallel without collisions.
---

# Slice to smallest value

Turn one ask into a short list of slices. Each slice ships on its own, a user can see it, and an automated check proves it.

## Rules

1. **Vertical, not horizontal.** A slice cuts through every layer it touches (UI, API, logic, storage) and ends in something a user or caller can observe. "Database schema", "backend only", "API layer" are not slices.
2. **First slice is the thinnest end-to-end path.** One input, one happy path, hard-coded where it is safe to be. Later slices widen it.
3. **Small.** At most 5 scope items and at most 5 acceptance criteria. Bigger means split.
4. **Every criterion names its proof.** A test name, a test file, `file::test`, or a backticked command. Never "manual", "works", a doc path, or a sentence in the PR body.
5. **Dependencies are explicit.** If slice B needs slice A merged first, B lists A in `depends_on`. Open issues outside the slice set go there too (`#40`, `owner/repo#7`, `PROJ-12`). No hidden ordering.
6. **Independent slices do not share files.** Two slices that can run at the same time must not touch the same file. If they must, add a dependency or merge them. Append-only files (a changelog) go in `shared_files`.
7. **Full coverage.** Every acceptance criterion of the parent is covered by at least one slice criterion. An idea you leave out goes in `parent.dropped` with a reason. Nothing is dropped silently.

## Procedure

1. Read the ask. Write down the parent's acceptance criteria as observable outcomes (`AC1`, `AC2`, ...). If the ask has none, derive them and state them as assumptions.
2. Find the thinnest end-to-end path that delivers any one criterion. That is `S1`.
3. Grow from `S1` with the splitting moves below until every parent criterion is covered.
4. For each slice, list the files you expect to touch. Read the code to get this right; guesses here cause merge collisions later.
5. Write the slice document (schema below) to a file, e.g. `slices.json`, or a markdown file with one fenced `json` block.
6. Run the checker. Fix every error. Re-run until it passes.
7. Optionally file the slices as issues with `file_slices.py` (see File). Creating issues is outward-facing: show the dry run and confirm with the user first unless they already asked you to file them.

## Splitting moves

Pick the one that keeps value visible:

- **Workflow steps:** ship step 1 end to end, then step 2.
- **Happy path, then edge cases:** valid input first; errors, limits, retries later.
- **One variant at a time:** one payment method, one file format, one role, one locale.
- **Simple rule, then full rule:** one flat shipping fee before per-region rates.
- **Defer non-functional work:** correct first, then fast, then scaled. Each is its own slice with a measurable criterion.
- **Read before write:** show existing data before editing it.
- **Unknowns:** if a slice cannot be specified, make a timeboxed spike slice whose criterion is a written answer plus a throwaway check, and make the real slices depend on it.

Setup work (a migration, a config flag) is folded into the first slice that needs it. It is not its own slice.

## Schema

```json
{
  "parent": {
    "id": "P1",
    "title": "Users can reset a forgotten password",
    "criteria": [{"id": "AC1", "text": "User can request a reset link by email"}],
    "dropped": [{"text": "Reset by SMS", "reason": "no SMS provider", "covered_by": "#40"}]
  },
  "shared_files": ["CHANGELOG.md"],
  "slices": [
    {
      "id": "S1",
      "title": "Request a link and set a new password",
      "value": "A locked-out user gets back in without support",
      "scope": ["Reset request form", "Email with token link"],
      "files": ["src/auth/reset.ts", "web/pages/reset.tsx"],
      "out_of_scope": ["Link expiry", "Rate limiting"],
      "context": "Tokens live in the existing sessions table.",
      "criteria": [
        {"id": "S1.1", "text": "Submitting a known email sends one link",
         "proof": "tests/auth/reset.test.ts::sends link for known email",
         "covers": ["AC1"]}
      ],
      "depends_on": []
    }
  ]
}
```

- `value`: one sentence, from the user's side. Required.
- `files`: paths, directories ending in `/`, or globs. Used for the overlap check.
- `out_of_scope`: what this slice will not do. Write real items; an empty list is a warning, and render prints no placeholder.
- `context`: optional background for whoever picks up the issue.
- `covers`: parent criterion ids this slice criterion satisfies.
- `depends_on`: slice ids that must merge first, or external issues (`#40`, `owner/repo#7`, `PROJ-12`).
- `shared_files`: append-only files (changelog, release notes) that independent slices may both touch.
- `parent.dropped`: ideas from the ask that get no slice, each with `text`, `reason`, and optional `covered_by`.

## Check

```
python3 "${CLAUDE_SKILL_DIR}/scripts/check_slices.py" slices.json
python3 "${CLAUDE_SKILL_DIR}/scripts/check_slices.py" slices.md --json
```

Errors (exit 1): more than 5 scope items or criteria, missing value or criteria, weak or missing proof, uncovered or unknown parent criterion, unknown or self dependency, dependency cycle, duplicate ids, independent slices sharing a file, dropped idea without a reason.

Warnings: title names a layer instead of value, slice covers no parent criterion, no scope items, no `out_of_scope` items, more than 10 files, a scope item or criterion over 200 characters or joined with `;`, a criterion about PR prose, parent has no criteria.

Options: `--overlap warn` downgrades file overlap to a warning (use only when slices will run one at a time). `--strict` fails on warnings.

It prints "touches N files across M directories" per slice, any dropped ideas, and the run order as layers. Slices in one layer can run in parallel; a slice with external dependencies is marked "waits on external #40". External ids are not part of the cycle or overlap checks.

How to fix common errors:

- `too-many-criteria` / `too-much-scope`: split with a move above. Do not merge criteria into one long sentence to dodge the limit.
- `many-files` / `packed-item` warnings: the slice is probably two. Split it, or confirm it is one change.
- `weak-proof`: name the test you will write (`test_expired_token_rejected`, `tests/reset.test.ts::expires`) or the command that proves it (`` `npm test -- reset` ``). A plain path counts only if it is a test file.
- `prose-criterion` warning: "the PR body records X" is not testable. Turn it into a check, or keep it out of the criteria.
- `file-overlap`: if one slice really builds on the other, add `depends_on`. If they are the same change, merge them. If the file is append-only (`CHANGELOG.md`), list it in `shared_files`. Otherwise split the shared file's change into the earlier slice.
- `uncovered`: add a slice or a criterion, or confirm with the user the parent criterion is dropped, remove it from the parent criteria, and record it in `parent.dropped`.
- `horizontal` warning: re-cut so the slice ends in observable behavior.

## Render

```
python3 "${CLAUDE_SKILL_DIR}/scripts/render.py" slices.json
python3 "${CLAUDE_SKILL_DIR}/scripts/render.py" slices.json --json --issue-map S1=41,S2=42 --parent-issue 40
```

Prints one markdown issue body per slice in run order, with Value, Context, Scope, Files, Acceptance criteria (each with its proof), Out of scope (only when listed), and Dependencies. External dependencies print as written. `--issue-map` rewrites slice ids to `#N` so "Depends on #41" links the real issues. `--parent-issue N` adds "Part of #N". `--show-covers` adds the parent criteria each criterion covers. Dropped ideas get no issue. Output goes to stdout only; nothing is posted.

## File

```
python3 "${CLAUDE_SKILL_DIR}/scripts/file_slices.py" slices.json --repo owner/name [--label L ...] [--parent-issue 40]
python3 "${CLAUDE_SKILL_DIR}/scripts/file_slices.py" slices.json --repo owner/name --create
```

Use this instead of filing rendered bodies by hand. It runs the checker first and stops on errors.

- **Dry run (default):** prints each issue in run order: title (the slice title), labels, readiness verdict, and body. A dependency on a slice that is not filed yet shows as `S1 (not created yet)`. Nothing is posted. Exit 1 if any body is not ready.
- **`--create`:** creating issues is outward-facing; confirm first. Needs `gh` (without it: `skipped: gh not found`, exit 2). It checks every body with check-issue-readiness and refuses before posting anything if one is not ready. It then creates one issue at a time with `gh issue create --repo ... --body-file ...`. Each body is rendered with the numbers created so far, so "Depends on #41" points at the real issue, and is checked again just before it is posted. Each new issue is re-checked with `check_issue.py --gh N`. The body never repeats the title. External dependencies print as written.
- **Map and resume:** after each create, slice id -> number and URL go to `--map-out` (default `slices.issues.json` next to the slices file). If a run stops (a not-ready body, a failed create), it lists what was already created. Re-run with `--create --resume` to skip those and finish without duplicates. `--create` without `--resume` refuses to start when the map already lists issues.
- Labels come only from `--label`. `--json` prints the plan and results.

## Judgment that stays with you

- Whether a slice is truly visible to a user, or just plumbing with a nice title.
- Which variant or path is thinnest. Prefer the one that exercises the riskiest layer early.
- Whether a dropped parent criterion is acceptable. Ask; do not decide alone.
- When the checker passes but the slices still feel big, split again.
