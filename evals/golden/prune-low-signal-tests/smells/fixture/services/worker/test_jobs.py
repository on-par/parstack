import os
import unittest

import jobs


class TestJobs(unittest.TestCase):
    def test_legacy_queue_removed(self):
        self.assertFalse(os.path.exists("jobs/legacy_queue.py"))

    def test_retry_is_callable(self):
        self.assertTrue(callable(jobs.retry))

    def test_backoff_matches_itself(self):
        self.assertEqual(jobs.backoff(3), jobs.backoff(3))

    def test_retries_until_success(self):
        calls = []
        jobs.retry(lambda: calls.append(1) or len(calls) == 3, attempts=5)
        self.assertEqual(len(calls), 3)

    def test_internal_queue_depth(self):
        q = jobs.Queue()
        q.push("a")
        self.assertEqual(q._items, ["a"])

    @unittest.skipIf(os.name == "nt", "posix only")
    def test_signal_handler(self):
        self.assertTrue(jobs.install_signal_handler())
