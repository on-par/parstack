"""Structural checks on the GitHub workflows.

Reads the workflow files as text (no YAML library) and checks the parts that
keep secrets and write tokens away from outside contributors.
"""
import json
import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
OWNER = "patrob"
FOREMAN_BOT = "on-par-foreman[bot]"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def block(text: str, key: str, level: int = 0) -> list[str]:
    """Non-blank lines nested under the first `key:` line at the given indent."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if indent(line) == level and line.strip() == f"{key}:":
            out = []
            for child in lines[i + 1:]:
                if not child.strip() or child.lstrip().startswith("#"):
                    continue
                if indent(child) <= level:
                    break
                out.append(child)
            return out
    raise AssertionError(f"no '{key}:' block at indent {level}")


def child_keys(lines: list[str]) -> list[str]:
    if not lines:
        return []
    base = min(indent(line) for line in lines)
    return [line.strip().split(":", 1)[0] for line in lines if indent(line) == base]


def job_if(text: str, job: str) -> str:
    """The job's `if:` expression, folded onto one line."""
    lines = block(text, job, level=2)
    for i, line in enumerate(lines):
        if indent(line) == 4 and line.strip().startswith("if:"):
            parts = [line.strip()[3:].strip()]
            for cont in lines[i + 1:]:
                if indent(cont) <= 4:
                    break
                parts.append(cont.strip())
            expr = " ".join(p for p in parts if p not in (">-", ">", "|"))
            m = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expr)
            return m.group(1) if m else expr
    raise AssertionError(f"job {job} has no if:")


# The close job's condition, written exactly one way so it can be checked here.
CLOSE_IF = re.compile(
    r"!contains\(fromJSON\('(?P<list>\[.*?\])'\), github\.event\.pull_request\.user\.login\)"
)


def closes(expr: str, login: str) -> bool:
    """Evaluate the close job's if-expression for a PR author.

    GitHub's contains() compares array items case-insensitively, so this does
    too. GitHub logins are case-insensitive, so 'PatRob' is the owner.
    """
    m = CLOSE_IF.fullmatch(expr)
    if not m:
        raise ValueError(f"unexpected close condition: {expr}")
    allow = json.loads(m.group("list"))
    return not any(item.lower() == login.lower() for item in allow)


class CloseOutsidePrsTest(unittest.TestCase):
    def setUp(self):
        self.text = read("close-outside-prs.yml")

    def test_trigger_is_pull_request_target_only(self):
        self.assertEqual(child_keys(block(self.text, "on")), ["pull_request_target"])
        self.assertIn("types: [opened, reopened]", self.text)

    def test_no_checkout_and_no_secrets(self):
        self.assertNotIn("actions/checkout", self.text)
        self.assertNotIn("secrets.", self.text)

    def test_permissions_exactly_pull_requests_write(self):
        self.assertEqual([l.strip() for l in block(self.text, "permissions")],
                         ["pull-requests: write"])
        self.assertEqual(len(re.findall(r"^\s*permissions:", self.text, re.M)), 1)

    def test_pr_fields_not_interpolated_into_run(self):
        for line in self.text.splitlines():
            if line.strip().startswith("run:"):
                self.assertNotIn("${{", line)

    def test_allowlist(self):
        expr = job_if(self.text, "close")
        self.assertFalse(closes(expr, OWNER))
        self.assertFalse(closes(expr, "dependabot[bot]"))
        self.assertFalse(closes(expr, FOREMAN_BOT))
        self.assertTrue(closes(expr, "other-app[bot]"))
        self.assertTrue(closes(expr, "random-user"))
        self.assertTrue(closes(expr, "patrob-fan"))
        # Case-insensitive like GitHub's contains(); logins ignore case too.
        self.assertFalse(closes(expr, "PatRob"))
        self.assertFalse(closes(expr, "PATROB"))

    def test_close_step(self):
        self.assertIn('gh pr close "$PR_NUMBER" --repo "$REPO" --comment "$MESSAGE"', self.text)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.text)


class BehaviorEvalsTest(unittest.TestCase):
    def setUp(self):
        self.text = read("behavior-evals.yml")

    def test_triggers(self):
        keys = child_keys(block(self.text, "on"))
        self.assertIn("pull_request", keys)
        self.assertIn("workflow_dispatch", keys)
        self.assertNotIn("pull_request_target", keys)
        self.assertNotRegex(self.text, r"(?m)^\s*pull_request_target:")

    def test_job_if_limits_to_owner_branches(self):
        expr = job_if(self.text, "behavior")
        self.assertIn("github.event_name == 'workflow_dispatch'", expr)
        self.assertIn("contains(github.event.pull_request.labels.*.name, 'run-evals')", expr)
        self.assertIn(f"github.event.pull_request.user.login == '{OWNER}'", expr)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", expr)
        # The eval secret stays owner-only; the bot allowed past auto-close
        # must not be able to trigger it.
        self.assertNotIn("on-par-foreman", expr)

    def test_auth_step_runs_first(self):
        steps = block(self.text, "steps", level=4)
        first = [l for l in steps if l.lstrip().startswith("- ")][0]
        self.assertIn("name: Check auth", first)
        auth = self.text.index("id: auth")
        self.assertLess(auth, self.text.index("actions/checkout"))
        self.assertLess(auth, self.text.index("actions/setup-node"))
        self.assertLess(auth, self.text.index("npm i -g @anthropic-ai/claude-code"))

    def test_later_steps_gated_on_auth(self):
        steps = block(self.text, "steps", level=4)
        starts = [i for i, l in enumerate(steps) if l.lstrip().startswith("- ")]
        for a, b in zip(starts[1:], starts[2:] + [len(steps)]):
            body = "\n".join(steps[a:b])
            self.assertIn("if: steps.auth.outputs.mode != 'none'", body)

    def test_run_steps_use_require_auth(self):
        self.assertEqual(self.text.count("--require-auth"), 2)
        self.assertNotIn("--require-api-key", self.text)
        self.assertNotIn("--bare", self.text)

    def test_api_key_never_reaches_oauth_runs(self):
        # Run steps pass the API key only in api_key mode; it would override
        # the OAuth token otherwise.
        for line in self.text.splitlines():
            if "secrets.ANTHROPIC_API_KEY" in line and "mode ==" in line:
                self.assertIn("steps.auth.outputs.mode == 'api_key' &&", line)
            if "secrets.CLAUDE_CODE_OAUTH_TOKEN" in line and "mode ==" in line:
                self.assertIn("steps.auth.outputs.mode == 'oauth' &&", line)

    def test_secrets_only_in_env(self):
        lines = self.text.splitlines()
        found = 0
        for i, line in enumerate(lines):
            if "secrets." not in line or line.lstrip().startswith("#"):
                continue
            found += 1
            self.assertRegex(line, r"^\s+[A-Z_][A-Z0-9_]*: \$\{\{.*\}\}\s*$")
            parent = next(p for p in reversed(lines[:i]) if p.strip() and indent(p) < indent(line))
            self.assertEqual(parent.strip(), "env:", f"line {i + 1} is not under env:")
        self.assertGreater(found, 0)


class CiTest(unittest.TestCase):
    def test_golden_evals_job(self):
        self.assertIn("golden-evals", child_keys(block(read("ci.yml"), "jobs")))


if __name__ == "__main__":
    unittest.main()
