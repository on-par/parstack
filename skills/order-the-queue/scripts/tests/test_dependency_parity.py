"""order-the-queue copies the body dependency rules of check-issue-readiness.

Skills stay self-contained, so the rules are mirrored rather than imported. This
test runs both scripts on the same bodies and fails if the rules drift apart.
It is skipped when check-issue-readiness is not installed next to this skill.
"""
import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
CHECK_ISSUE = HERE.parents[2] / "check-issue-readiness" / "scripts" / "check_issue.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oq = load("parstack_order_queue_parity", HERE.parent / "order_queue.py")


@unittest.skipUnless(CHECK_ISSUE.is_file(), "check-issue-readiness not installed alongside")
class DependencyParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci = load("parstack_check_issue_parity", CHECK_ISSUE)
        cls.cases = json.loads((FIXTURES / "dependency_cases.json").read_text(encoding="utf-8"))

    def check_issue_refs(self, body):
        return self.ci.parse_issue({"title": "x", "body": body})["dependencies"]

    def test_both_agree_with_the_expected_refs(self):
        for case in self.cases:
            with self.subTest(case["name"]):
                self.assertEqual(self.check_issue_refs(case["body"]), case["refs"])
                self.assertEqual(oq.body_refs(case["body"]), case["refs"])

    def test_tracker_keys_match_but_are_not_queue_edges(self):
        body = "Depends on NOTES-7 and #12"
        self.assertEqual(oq.body_refs(body), self.check_issue_refs(body))
        local, external = oq.parse_deps({"body": body, "labels": []})
        self.assertEqual((local, external), ([12], []))


if __name__ == "__main__":
    unittest.main()
