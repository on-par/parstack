import contextlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "make_trigger_workflow.py")
FIXTURES = os.path.join(HERE, "fixtures")

_spec = importlib.util.spec_from_file_location("parstack_make_trigger_workflow", SCRIPT)
mtw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mtw)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mtw.main(argv)
    return code, out.getvalue(), err.getvalue()


def generate(**kw):
    defaults = dict(
        name="agent-run",
        label=None,
        command=None,
        crons=[],
        run_command=mtw.DEFAULT_RUN_COMMAND,
        running_label="running",
        timeout_minutes=60,
    )
    defaults.update(kw)
    return mtw.build_workflow(**defaults)


def run_blocks(text):
    """Return the text of every run: script (inline or block scalar)."""
    lines = text.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\s*)(- )?run:\s*(.*)$", line)
        if not m:
            i += 1
            continue
        indent = len(m.group(1)) + (2 if m.group(2) else 0)
        value = m.group(3)
        if value.startswith(("|", ">")):
            body = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                body.append(lines[i])
                i += 1
            blocks.append("\n".join(body))
        else:
            blocks.append(value)
            i += 1
    return blocks


class BuildWorkflowTest(unittest.TestCase):
    def test_all_triggers_present(self):
        text = generate(label="agent:run", command="/run", crons=["0 6 * * 1"])
        self.assertIn("  issues:\n    types: [labeled]", text)
        self.assertIn("  issue_comment:\n    types: [created]", text)
        self.assertIn('  schedule:\n    - cron: "0 6 * * 1"', text)
        self.assertIn("github.event.label.name == 'agent:run'", text)
        self.assertIn("github.event.comment.body == '/run'", text)
        self.assertIn("startsWith(github.event.comment.body, '/run ')", text)

    def test_comment_restricted_to_trusted_associations(self):
        text = generate(command="/run")
        self.assertIn("github.event.comment.author_association", text)
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
            self.assertIn(f'"{assoc}"', text)
        self.assertNotIn("CONTRIBUTOR", text)
        self.assertNotIn("NONE", text)

    def test_concurrency_blocks_double_start(self):
        text = generate(label="go")
        self.assertIn("concurrency:\n  group: ${{ github.workflow }}-${{ github.event.issue.number || 'scheduled' }}", text)
        self.assertIn("cancel-in-progress: false", text)

    def test_guard_adds_and_always_releases_running_label(self):
        text = generate(label="go", running_label="busy")
        self.assertIn("- name: Guard against double start", text)
        self.assertIn('RUNNING_LABEL: "busy"', text)
        self.assertIn('grep -Fxq "$RUNNING_LABEL"', text)
        self.assertIn('-X POST "repos/$REPO/issues/$NUMBER/labels"', text)
        release = text.split("- name: Release running label", 1)[1]
        self.assertIn("if: always() && steps.guard.outputs.acquired == 'true'", release)
        self.assertIn("-X DELETE", release)
        run_step = text.split("- name: Run\n", 1)[1].split("- name:", 1)[0]
        self.assertIn("steps.guard.outputs.acquired == 'true'", run_step)

    def test_minimal_permissions(self):
        text = generate(crons=["*/30 * * * *"])
        self.assertIn("permissions: {}", text)
        self.assertIn("      contents: read", text)
        self.assertNotIn("issues: write", text)
        self.assertNotIn("pull-requests: write", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("Guard against double start", text)
        label_only = generate(label="go")
        self.assertIn("issues: write", label_only)
        self.assertNotIn("pull-requests: write", label_only)

    def test_untrusted_fields_only_in_env_or_if(self):
        text = generate(label="go", command="/run", crons=["0 0 * * *"])
        for block in run_blocks(text):
            self.assertNotIn("${{", block)
        for line in text.splitlines():
            for field in mtw.UNTRUSTED_FIELDS:
                if field in line and "${{" in line:
                    self.assertRegex(line, r"^\s+[A-Z_]+: \$\{\{ " + re.escape(field) + r" \}\}$")
        self.assertIn("COMMENT_BODY: ${{ github.event.comment.body }}", text)
        self.assertIn("ISSUE_TITLE: ${{ github.event.issue.title }}", text)
        self.assertEqual(mtw.lint_workflow(text), [])

    def test_run_command_passed_via_env(self):
        cmd = './agent.sh "$ISSUE_NUMBER" && echo \'done\''
        text = generate(label="go", run_command=cmd)
        self.assertIn("RUN_COMMAND: " + json.dumps(cmd), text)
        self.assertIn('run: bash -c "$RUN_COMMAND"', text)
        self.assertEqual(run_blocks(text).count('bash -c "$RUN_COMMAND"'), 1)
        self.assertIn('RUN_COMMAND: "echo \\"replace with your run command\\""', generate(label="go"))


class LintTest(unittest.TestCase):
    def test_flags_injectable_fixture(self):
        with open(os.path.join(FIXTURES, "unsafe.yml"), encoding="utf-8") as fh:
            problems = mtw.lint_workflow(fh.read())
        self.assertTrue(any("expression inside run" in p for p in problems), problems)
        self.assertTrue(any("github.event.issue.title" in p for p in problems), problems)

    def test_lint_cli_exit_codes(self):
        code, out, _ = run_main(["--lint", os.path.join(FIXTURES, "unsafe.yml"), "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ok.yml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(generate(command="/go"))
            code, out, _ = run_main(["--lint", path])
            self.assertEqual(code, 0)
            self.assertIn("ok", out)


class CliTest(unittest.TestCase):
    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_main(["--label", "go", "--repo", tmp])
            self.assertEqual(code, 0)
            self.assertIn("name: agent-run", out)
            self.assertFalse(os.path.exists(os.path.join(tmp, ".github")))

    def test_write_and_refuse_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_main(["--name", "kick", "--command", "/kick", "--repo", tmp, "--write", "--json"])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertTrue(data["written"])
            path = os.path.join(tmp, ".github", "workflows", "kick.yml")
            self.assertEqual(data["path"], path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), data["yaml"])
            code, _, err = run_main(["--name", "kick", "--command", "/kick", "--repo", tmp, "--write"])
            self.assertEqual(code, 2)
            self.assertIn("--force", err)
            code, _, _ = run_main(["--name", "kick", "--command", "/kick", "--repo", tmp, "--write", "--force"])
            self.assertEqual(code, 0)

    def test_usage_errors(self):
        bad = [
            [],
            ["--label", "it's"],
            ["--command", "run"],
            ["--command", "/run; rm"],
            ["--cron", "* * *"],
            ["--label", "go", "--run-command", "echo ${{ github.event.comment.body }}"],
            ["--label", "go", "--name", "Bad Name"],
            ["--label", "running"],
        ]
        for argv in bad:
            code, _, err = run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
