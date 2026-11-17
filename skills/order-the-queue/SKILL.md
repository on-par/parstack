---
name: order-the-queue
description: Turn a set of open issues into a run order of parallel groups based on their declared dependencies, and flag cycles, missing or closed dependencies, blocked items, and stale items. Use when deciding what to work on next, when feeding several issues to parallel agents or people, or when a backlog's dependency links need a health check.
---

# Order the queue

Read open issues, build the dependency graph, and print groups that can run in parallel. Group 1 has no open dependencies. Group 2 depends only on group 1. And so on.

## Run

```
python3 "${CLAUDE_SKILL_DIR}/scripts/order_queue.py" --gh [--repo owner/name] [--limit 200]
python3 "${CLAUDE_SKILL_DIR}/scripts/order_queue.py" --file issues.json
python3 "${CLAUDE_SKILL_DIR}/scripts/order_queue.py" --file queue.md
```

Options:

- `--priority-label p0,p1,p2`: order issues inside a group by the first matching label, highest first. Ties break by issue number (oldest first).
- `--blocked-label blocked,on-hold`: labels that mark an issue blocked (default `blocked`).
- `--stale-days N`: flag issues not updated in N days (default 30).
- `--no-resolve`: with `--gh`, skip looking up dependencies that are not in the fetched list.
- `--now 2026-01-01T00:00:00Z`: fix the clock for reproducible output.
- `--json`: machine output.

`--gh` needs the `gh` CLI. If it is missing the script prints `skipped: gh not found` and exits 2. Export the issues to a file and use `--file` instead.

Exit codes: 0 ordered, 1 the graph has a cycle or an unknown dependency, 2 input error or skipped.

## Inputs

**JSON:** a list (or `{"issues": [...]}`) of objects with `number`, `title`, `body`, `labels`, `updatedAt`, `state`. This is the shape `gh issue list --json` returns. Closed issues in the list count as finished dependencies.

**Markdown:**

```markdown
- #20 Add search box (labels: p1, search) (updated: 2026-11-01)
- #21 Search results page (depends on #20)
- #22 Search filters
  Blocked by #20 and #21.
- [x] #19 Finished item
```

Indented lines belong to the item above. `[x]` marks a closed item.

## How dependencies are read

- Body phrases: "depends on #N", "blocked by #N", "blocked on #N", "requires #N", "after #N". Lists work: "depends on #3, #4 and #5". Code blocks and inline code are ignored.
- Labels: `depends-on-12`, `blocked-by:12`, `after #12`.
- `owner/repo#N` is reported as external and not checked.

A dependency that is:

- **in the list** orders the issue after it.
- **closed** is satisfied. It is reported so the stale reference can be removed.
- **open but not in the list** (found via `gh issue view`) blocks the issue. Raise `--limit` or include it.
- **unknown** blocks the issue and makes the exit code 1.

## Output

- **run order:** groups of issues that can run at the same time.
- **blocked:** issues left out of the run order, each with reasons: blocking label, unknown or unlisted dependency, in a cycle, or depending on a blocked issue.
- **cycles:** each cycle as `#7 -> #8 -> #7`.
- **missing:** dependencies that are closed, unknown, open-not-listed, or external.
- **stale:** issues with no update in more than `--stale-days` days. They stay in the run order; you decide.

## Procedure

1. Run the script. Read blocked, cycles, and missing before the run order.
2. Fix the graph where the fix is clear: remove references to closed issues, correct a wrong issue number. Editing issues is outward-facing; confirm with the user before changing remote issues.
3. For each cycle, find which link is wrong or which issue should be split so the cycle breaks. Propose it; do not guess.
4. For stale items, propose: close, refresh, or keep. Do not close issues without confirmation.
5. Hand out work group by group. Start group N+1 items only when their dependencies are merged, not just opened as PRs.
6. Before starting any issue, check it is ready (criteria, proof, scope, size).

## Judgment that stays with you

- Tie-breaks the labels do not settle: risk first, then value, then size.
- What to drop or defer from the queue.
- Whether two issues in the same group will collide on the same files. The graph only knows declared dependencies; if two parallel issues touch the same code, add a dependency or run them one after the other.
- Whether a missing dependency is real or just a stale mention.
