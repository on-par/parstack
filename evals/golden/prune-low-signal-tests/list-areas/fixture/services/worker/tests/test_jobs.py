import unittest

from worker import jobs


class TestJobs(unittest.TestCase):
    def test_runs_queued_job(self):
        self.assertEqual(jobs.run_next(["a"]), "a")

    def test_empty_queue_returns_none(self):
        self.assertIsNone(jobs.run_next([]))
