import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate  # noqa: E402

MARKETPLACE = {
    "name": "parstack",
    "owner": {"name": "On PAR"},
    "plugins": [{"name": "parstack", "source": "./"}],
}
PLUGIN = {"name": "parstack", "version": "0.1.0"}

GOOD_SKILL_MD = """---
name: demo-skill
description: Use when you need a demo.
---

# demo-skill

Run `python3 "${CLAUDE_SKILL_DIR}/scripts/run.py" --json`.
"""


class RepoFixture:
    """Build a minimal valid repo in a temp dir; tests then break one thing."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write_json(".claude-plugin/marketplace.json", MARKETPLACE)
        self.write_json(".claude-plugin/plugin.json", PLUGIN)
        self.write("README.md", "# parstack\n")

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, rel: str, data: object) -> Path:
        return self.write(rel, json.dumps(data))

    def add_skill(self, name: str = "demo-skill", skill_md: str | None = None) -> Path:
        md = GOOD_SKILL_MD if skill_md is None else skill_md
        self.write(f"skills/{name}/SKILL.md", md.replace("demo-skill", name))
        self.write(f"skills/{name}/scripts/run.py", "import json\nimport helper\n")
        self.write(f"skills/{name}/scripts/helper.py", "import os\n")
        for d in ("", "scripts/", "scripts/tests/"):
            self.write(f"skills/{name}/{d}__init__.py", "")
        self.write(
            f"skills/{name}/scripts/tests/test_run.py",
            "import unittest\nimport run\n",
        )
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.write("README.md", readme + f"- {name}\n")
        return self.root / "skills" / name

    def validate(self) -> validate.Report:
        return validate.validate(self.root)


class ValidateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = RepoFixture()
        self.addCleanup(self.repo.cleanup)

    def assertProblem(self, report: validate.Report, fragment: str) -> None:
        self.assertTrue(
            any(fragment in p for p in report.problems),
            f"expected a problem containing {fragment!r}, got {report.problems}",
        )


class TestEmptyRepo(ValidateTestCase):
    def test_no_skills_dir_passes_with_warning(self):
        report = self.repo.validate()
        self.assertEqual(report.problems, [])
        self.assertTrue(any("no skills/" in w for w in report.warnings))

    def test_empty_skills_dir_passes_with_warning(self):
        (self.repo.root / "skills").mkdir()
        report = self.repo.validate()
        self.assertEqual(report.problems, [])
        self.assertTrue(any("no skill dirs" in w for w in report.warnings))


class TestValidSkill(ValidateTestCase):
    def test_valid_skill_is_clean(self):
        self.repo.add_skill()
        report = self.repo.validate()
        self.assertEqual(report.problems, [])
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.skills, ["demo-skill"])

    def test_folded_description(self):
        md = "---\nname: demo-skill\ndescription: >\n  Use when\n  folded.\n---\n"
        self.repo.add_skill(skill_md=md)
        self.assertEqual(self.repo.validate().problems, [])


class TestSkillFrontmatter(ValidateTestCase):
    def test_missing_skill_md(self):
        (self.repo.root / "skills" / "empty-skill").mkdir(parents=True)
        self.assertProblem(self.repo.validate(), "missing SKILL.md")

    def test_missing_frontmatter(self):
        self.repo.add_skill(skill_md="# demo-skill\n")
        self.assertProblem(self.repo.validate(), "missing YAML frontmatter")

    def test_name_mismatch(self):
        skill = self.repo.add_skill()
        (skill / "SKILL.md").write_text(
            "---\nname: other\ndescription: x\n---\n", encoding="utf-8"
        )
        self.assertProblem(self.repo.validate(), "must equal dir name")

    def test_name_missing(self):
        skill = self.repo.add_skill()
        (skill / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
        self.assertProblem(self.repo.validate(), "needs `name`")

    def test_non_kebab_dir(self):
        self.repo.add_skill(name="Bad_Name")
        self.assertProblem(self.repo.validate(), "lowercase kebab-case")

    def test_empty_description(self):
        self.repo.add_skill(skill_md="---\nname: demo-skill\ndescription: \"\"\n---\n")
        self.assertProblem(self.repo.validate(), "non-empty `description`")

    def test_description_too_long(self):
        md = f"---\nname: demo-skill\ndescription: {'x' * 1025}\n---\n"
        self.repo.add_skill(skill_md=md)
        self.assertProblem(self.repo.validate(), "max 1024")

    def test_description_at_limit_ok(self):
        md = f"---\nname: demo-skill\ndescription: {'x' * 1024}\n---\n"
        self.repo.add_skill(skill_md=md)
        self.assertEqual(self.repo.validate().problems, [])


class TestRootAndManifests(ValidateTestCase):
    def test_root_skill_md_rejected(self):
        self.repo.write("SKILL.md", "---\nname: x\ndescription: y\n---\n")
        self.assertProblem(self.repo.validate(), "must not exist at the repo root")

    def test_marketplace_bad_json(self):
        self.repo.write(".claude-plugin/marketplace.json", "{not json")
        self.assertProblem(self.repo.validate(), "marketplace.json does not parse")

    def test_marketplace_missing(self):
        (self.repo.root / ".claude-plugin" / "marketplace.json").unlink()
        self.assertProblem(self.repo.validate(), "marketplace.json is missing")

    def test_marketplace_wrong_source(self):
        data = json.loads(json.dumps(MARKETPLACE))
        data["plugins"][0]["source"] = "./plugins/parstack"
        self.repo.write_json(".claude-plugin/marketplace.json", data)
        self.assertProblem(self.repo.validate(), 'source must be "./"')

    def test_marketplace_missing_plugin(self):
        data = json.loads(json.dumps(MARKETPLACE))
        data["plugins"] = [{"name": "other", "source": "./"}]
        self.repo.write_json(".claude-plugin/marketplace.json", data)
        self.assertProblem(self.repo.validate(), 'must list plugin "parstack"')

    def test_plugin_name_mismatch(self):
        self.repo.write_json(".claude-plugin/plugin.json", {"name": "other"})
        self.assertProblem(self.repo.validate(), "plugin.json name must be")

    def test_plugin_bad_json(self):
        self.repo.write(".claude-plugin/plugin.json", "[")
        self.assertProblem(self.repo.validate(), "plugin.json does not parse")


class TestScripts(ValidateTestCase):
    def test_missing_referenced_script(self):
        skill = self.repo.add_skill()
        (skill / "scripts" / "run.py").unlink()
        self.assertProblem(self.repo.validate(), "scripts/run.py does not exist")

    def test_reference_with_trailing_punctuation(self):
        md = GOOD_SKILL_MD + "\nSee ${CLAUDE_SKILL_DIR}/scripts/helper.py.\n"
        self.repo.add_skill(skill_md=md)
        self.assertEqual(self.repo.validate().problems, [])

    def test_reference_escaping_scripts_dir(self):
        md = GOOD_SKILL_MD + "\n${CLAUDE_SKILL_DIR}/scripts/../SKILL.md\n"
        self.repo.add_skill(skill_md=md)
        self.assertProblem(self.repo.validate(), "escapes scripts/")

    def test_non_stdlib_import(self):
        skill = self.repo.add_skill()
        (skill / "scripts" / "run.py").write_text("import requests\n", encoding="utf-8")
        self.assertProblem(self.repo.validate(), "non-stdlib module 'requests'")

    def test_non_stdlib_from_import(self):
        skill = self.repo.add_skill()
        (skill / "scripts" / "run.py").write_text("from yaml import safe_load\n", encoding="utf-8")
        self.assertProblem(self.repo.validate(), "non-stdlib module 'yaml'")

    def test_relative_import_allowed(self):
        skill = self.repo.add_skill()
        (skill / "scripts" / "run.py").write_text("from . import helper\n", encoding="utf-8")
        self.assertEqual(self.repo.validate().problems, [])

    def test_test_may_import_parent_scripts_module(self):
        # fixture test_run.py imports `run` from the parent scripts dir
        self.repo.add_skill()
        self.assertEqual(self.repo.validate().problems, [])

    def test_cross_skill_import_rejected(self):
        self.repo.add_skill(name="first-skill")
        other = self.repo.add_skill(name="second-skill")
        (other / "scripts" / "run.py").write_text("import helper_from_first\n", encoding="utf-8")
        self.repo.write("skills/first-skill/scripts/helper_from_first.py", "")
        self.assertProblem(self.repo.validate(), "non-stdlib module 'helper_from_first'")

    def test_syntax_error_reported(self):
        skill = self.repo.add_skill()
        (skill / "scripts" / "run.py").write_text("def (:\n", encoding="utf-8")
        self.assertProblem(self.repo.validate(), "does not parse")

    def test_missing_init_for_discovery(self):
        skill = self.repo.add_skill()
        (skill / "scripts" / "tests" / "__init__.py").unlink()
        self.assertProblem(self.repo.validate(), "needs __init__.py")


class TestReadmeWarning(ValidateTestCase):
    def test_skill_missing_from_readme_warns(self):
        self.repo.add_skill()
        self.repo.write("README.md", "# parstack\n")
        report = self.repo.validate()
        self.assertEqual(report.problems, [])
        self.assertTrue(any("not mentioned in README.md" in w for w in report.warnings))


class TestFrontmatterParser(unittest.TestCase):
    def test_none_without_block(self):
        self.assertIsNone(validate.parse_frontmatter("# title\n"))
        self.assertIsNone(validate.parse_frontmatter("---\nname: x\n"))

    def test_quoted_and_plain(self):
        fm = validate.parse_frontmatter(
            "---\nname: 'a-b'\ndescription: \"Use when: x\"\nother: plain # c\n---\n"
        )
        self.assertEqual(fm, {"name": "a-b", "description": "Use when: x", "other": "plain"})

    def test_literal_block(self):
        fm = validate.parse_frontmatter("---\ndescription: |\n  one\n  two\n---\n")
        self.assertEqual(fm["description"], "one\ntwo")

    def test_plain_continuation(self):
        fm = validate.parse_frontmatter("---\ndescription: one\n  two\n---\n")
        self.assertEqual(fm["description"], "one two")

    def test_nested_mapping_not_a_string(self):
        fm = validate.parse_frontmatter("---\nmetadata:\n  k: v\nname: x\n---\n")
        self.assertNotIsInstance(fm["metadata"], str)
        self.assertEqual(fm["name"], "x")


class TestMain(ValidateTestCase):
    def run_main(self, *args: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = validate.main(list(args))
        return code, buf.getvalue()

    def test_exit_zero_and_json(self):
        self.repo.add_skill()
        code, out = self.run_main("--root", str(self.repo.root), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["skills"], ["demo-skill"])

    def test_exit_one_on_problem(self):
        self.repo.write("SKILL.md", "x")
        code, out = self.run_main("--root", str(self.repo.root))
        self.assertEqual(code, 1)
        self.assertIn("error:", out)
        self.assertIn("FAIL", out)

    def test_exit_two_on_bad_root(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code, _ = self.run_main("--root", str(self.repo.root / "nope"))
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err.getvalue())


if __name__ == "__main__":
    unittest.main()
