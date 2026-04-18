"""Unit tests for dispatcher + mock backend.

Run manually from the v2 directory:
    cd C:\\MirageWork\\mcp-server-v2
    python test_dispatcher.py

Expected: all tests print OK.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import Job
from backend_mock import MockBackend
from dispatcher import Dispatcher, reset_default
from parallel import CLI_GATE


def mk_job(job_id='j1', prompt='hi', timeout=60):
    return Job(job_id=job_id, kind='task', prompt=prompt, cwd='.', timeout_sec=timeout)


class TestDispatcher(unittest.TestCase):

    def tearDown(self):
        reset_default(None)

    def test_submit_and_complete(self):
        d = Dispatcher(MockBackend(delay_sec=0.05, output='hello'), max_workers=2)
        jid = d.submit(mk_job())
        # Wait up to 2s for completion
        for _ in range(40):
            if d.status(jid) == 'done':
                break
            time.sleep(0.05)
        self.assertEqual(d.status(jid), 'done')
        r = d.result(jid)
        self.assertIsNotNone(r)
        self.assertTrue(r.ok)
        self.assertIn('hello', r.output)
        d.shutdown()

    def test_parallel_execution(self):
        # Mock that sleeps 0.3s per job; with 3 workers, 3 jobs should finish
        # in ~0.3s, not ~0.9s.
        d = Dispatcher(MockBackend(delay_sec=0.3), max_workers=3)
        start = time.monotonic()
        ids = [d.submit(mk_job(job_id=f'p{i}')) for i in range(3)]
        for jid in ids:
            for _ in range(40):
                if d.status(jid) == 'done':
                    break
                time.sleep(0.05)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0, f'parallel should complete fast, took {elapsed:.2f}s')
        for jid in ids:
            self.assertEqual(d.status(jid), 'done')
        d.shutdown()

    def test_gate_bounds_concurrency(self):
        # Gate capacity is 3 by default. With 10 simultaneous 0.2s jobs and
        # 10 workers, total elapsed should still be about ceil(10/3)*0.2 = 0.8s,
        # not 0.2s (which would require 10 real parallel slots).
        d = Dispatcher(MockBackend(delay_sec=0.2), max_workers=10)
        start = time.monotonic()
        ids = [d.submit(mk_job(job_id=f'g{i}')) for i in range(10)]
        for jid in ids:
            for _ in range(60):
                if d.status(jid) == 'done':
                    break
                time.sleep(0.05)
        elapsed = time.monotonic() - start
        # With capacity 3, 10 jobs x 0.2s -> ~0.8s lower bound (0.6 minimum)
        self.assertGreater(elapsed, 0.4, f'gate should serialise, elapsed {elapsed:.2f}s')
        d.shutdown()

    def test_failure_result(self):
        d = Dispatcher(MockBackend(delay_sec=0.01, ok=False, output='boom'))
        jid = d.submit(mk_job())
        for _ in range(40):
            if d.status(jid) == 'done':
                break
            time.sleep(0.05)
        r = d.result(jid)
        self.assertIsNotNone(r)
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, 1)
        d.shutdown()

    def test_duplicate_submit_is_idempotent(self):
        d = Dispatcher(MockBackend(delay_sec=0.05))
        jid1 = d.submit(mk_job())
        jid2 = d.submit(mk_job())          # same job_id by default
        self.assertEqual(jid1, jid2)
        # Still only one entry in stats
        for _ in range(40):
            if d.status(jid1) == 'done':
                break
            time.sleep(0.05)
        s = d.stats()
        self.assertEqual(s['total'], 1)
        d.shutdown()

    def test_stats_snapshot(self):
        d = Dispatcher(MockBackend(delay_sec=0.2), max_workers=2)
        d.submit(mk_job(job_id='s1'))
        d.submit(mk_job(job_id='s2'))
        d.submit(mk_job(job_id='s3'))
        s = d.stats()
        self.assertEqual(s['total'], 3)
        self.assertEqual(s['backend'], 'mock')
        self.assertIn(s['gate_capacity'], (1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
        d.shutdown()


if __name__ == '__main__':
    unittest.main(verbosity=2)
