---
name: prune-low-signal-tests
description: Find tests that do not protect behaviour and say what to do with each. Use when a suite has grown slow or noisy, when tests break on refactors that change no behaviour, after a feature or module is removed, or when reviewing the tests in a PR. Sorts candidates into past, can't-fail, implementation-coupled, and duplicate, gives each a delete or rewrite disposition, and lists borderline tests kept on purpose. Reports by default; deletes or rewrites only when asked, one area at a time.
---

# Prune low-signal tests

A test earns its place when it tracks behaviour: it has to change when the behaviour it guards changes, and it has to fail when that behaviour breaks. A test that does neither still costs run time, review time, and false alarms on every refactor. This skill finds those tests and reports them. The script does the listing and the cheap pattern matching. You read each test and decide.

## The keep rule

Keep a test only when both are true:

1. It changes when the behaviour it guards changes.
2. It fails when that behaviour regresses.

Everything else is a candidate. A candidate is not a verdict: read the test and the code it calls before you put it in a category.

## Categories

| Category | What it looks like | Usual disposition |
|---|---|---|
| past | Guards code that was removed or moved. Asserts that a file, export, flag, route, or name is gone. Points at a module that no longer exists. | delete |
| can't-fail | No code change can make it fail. Compares a value with itself or with the same call. Reads back literals from a module that only holds types. Checks what the type system or language already guarantees (a function is a function, a required field exists). Sits behind a skip or condition that is now never true. | delete |
| implementation-coupled | Breaks on refactors that keep behaviour the same. Recomputes the expected value with the code's own logic. Reads private state. Asserts internal call order or how many times a helper ran. Pins a whole copied style or class string. | rewrite against what callers rely on, or delete if a behaviour test already covers it |
| duplicate | Another test already guards the same behaviour with the same inputs. Name that other test. | delete the weaker one |

Each candidate gets exactly one disposition:

- `delete`: nothing worth keeping.
- `rewrite`: the behaviour matters but the test checks it the wrong way. Say in one line what the rewritten test should assert.

Also list production code that exists only for the candidates: test-only exports, hooks, flags, or helpers that nothing else calls. Deleting the tests leaves that code dead.

## Scope

Work on one area (a directory or package) or one PR diff per pass. Never sweep the whole repo at once: the report gets too long to read carefully, and a wrong call spreads.

- Use `list` to see the areas and pick one.
- For a PR, use `diff` to see which test files and test names it adds or removes, and review only those.

## Commands

```
python3 "${CLAUDE_SKILL_DIR}/scripts/scan_tests.py" list [PATH] [--depth 2]           # test files by area, with counts
python3 "${CLAUDE_SKILL_DIR}/scripts/scan_tests.py" diff --base origin/main           # tests changed in base...HEAD
python3 "${CLAUDE_SKILL_DIR}/scripts/scan_tests.py" diff --diff-file changes.diff     # or a saved unified diff; - reads stdin
python3 "${CLAUDE_SKILL_DIR}/scripts/scan_tests.py" smells [PATH]                     # static hints per test file
```

Every mode takes `--json`. Output is sorted, so two runs on the same tree match.

- `list` recognises `*.test.*`, `*.spec.*` and `__tests__/` (JS/TS), `test_*.py` and `*_test.py`, `*_test.go`, `*Test(s).cs|java|kt|php|swift`, `*_spec.rb`, `*_test.rb`, and `*_test.exs`. An area is the first `--depth` directory segments under PATH. Inside a git work tree it respects `.gitignore`; otherwise it skips dependency and build dirs.
- `diff` reports each changed test file with its status (added, deleted, modified, renamed) and the tests added or removed. A line edited without renaming its test does not count. It also lists non-test files deleted or moved, because tests that still mention them are likely past.
- Test names are best effort: `it`/`test`/`describe` in JS/TS, `def test_*` and test classes in Python, `func Test*` in Go. Other languages are listed with a null test count.
- `smells` flags lines worth reading. Hint kinds and the category they often point to:

| Hint | Often points to |
|---|---|
| `absence-assertion` (`existsSync(...)).toBe(false)`, `.not.toContain`, `assertNotIn`, `assert not hasattr`) | past, or a security negative to keep |
| `self-comparison` (`expect(x).toEqual(x)`, `assertEqual(f(), f())`) | can't-fail |
| `type-only-check` (`typeof x === 'function'`, `assert callable(x)`) | can't-fail |
| `skipped-or-gated` (`it.skip`, `skipIf`, `@pytest.mark.skipif`, `t.Skip`) | can't-fail when the condition is now never true |
| `call-count` (`toHaveBeenCalledTimes`, `call_count`, `assert_called_once`) | implementation-coupled |
| `private-access` (`(x as any).y`, `obj._field`) | implementation-coupled |
| `copied-class-string` (a class or style assertion with a long literal) | implementation-coupled |

A hint is a place to look, not a finding. Many hinted lines are fine, and many candidates have no hint.

Exit codes: 0 ok (hints never fail the run), 2 usage or input error (with `--json`, stdout holds `{"status": "error", "message": ...}`). A bad `--base` ref or a missing diff file is an error, never an empty report.

## Workflow

1. Pick the scope: one area from `list`, or one PR with `diff`.
2. Find the coverage command (from CI, `package.json`, `pyproject.toml`, the verify script). Record the command and its baseline numbers before anything is deleted. If there is no coverage command, say so in the report.
3. Run the test suite for the scope. A failing test is never a pruning candidate. Report it as a possible bug and leave it alone.
4. Run `smells` on the scope, then read every test file in it, hinted or not. Apply the keep rule to each test.
5. Write the report (format below). Stop here unless the user asked for changes.
6. If the user explicitly asks you to delete or rewrite, work one area at a time: apply that area's dispositions, delete the production code that existed only for those tests, run the tests and the coverage command again, and compare with the baseline. If coverage of real code dropped, a deleted test was guarding something: put it back or rewrite it. Never lower a coverage threshold to make the numbers pass.

## Kept on purpose

Some tests look like candidates and are not. List every borderline test you kept, with one line on why. Common reasons:

- The call at the boundary is the behaviour: a payment provider charged once, an email sent once, an audit event written.
- A security negative: a secret, token, or card number never appears in logs, errors, or responses.
- Compatibility: old stored data, old config files, or old API payloads still load.
- A deliberate pin or budget: a size limit, a performance ceiling, a snapshot someone reviews on purpose.
- A user-facing rejection: a retired option or flag still gives a clear error instead of being silently ignored.

## Report format

```
Scope: <area or PR>   Coverage: <command>, baseline <numbers> (or "no coverage command found")

Totals
  past: N   can't-fail: N   implementation-coupled: N   duplicate: N
  delete: N   rewrite: N

## Past
path/to/file.test.ts > "test name" | delete | asserts the old export is gone; module removed in an earlier change

## Can't-fail
...

## Implementation-coupled
path/to/file.test.ts > "test name" | rewrite | counts helper calls; assert the returned total instead

## Duplicate
path/to/file.test.ts > "test name" | delete | same input and check as "other test name" in other_file.test.ts

## Production code only used by candidates
path/to/module.ts: exportForTests

## Kept on purpose
path/to/file.test.ts > "test name" | security negative: the card number must never reach the receipt

## Failing tests (not pruned)
path/to/file.test.ts > "test name" | fails on the current tree; possible bug
```

For an empty section, keep the heading and write `none`, so a reader can tell it was checked.

## Judgment

- When unsure between delete and rewrite, choose rewrite and say what behaviour the new test should check.
- A test that is hard to read is not low-signal for that reason alone. Judge what it guards, not how it looks.
- For a duplicate, keep the test that checks behaviour through the public interface, and delete the one that reaches into internals.
- Deleting a test never needs a lower coverage threshold. If the threshold fails after pruning, the pruned test was covering real code.

Related: shrink-quality-budgets holds the line on dead code once the test-only production code is gone; triage-failing-checks helps decide whether a failing test is this change's fault before you report it.
