"""Shared concurrency gate for v2 Claude Code CLI subprocess tasks.

Motivation
----------
run_task and run_loop / run_loop_v2 both spawn Claude Code CLI subprocesses
via subprocess.run. There was no upper bound on how many could run at once:
each call just started a fresh daemon thread. When Jun kicked off more than a
handful of concurrent jobs, the MCP server would thrash (CLI startup cost,
OAuth credential contention, Windows handle pressure), which is the origin of
the "10+ parallel tasks overloads MCP" note in userMemories.

Design
------
One BoundedSemaphore, shared across task.py and loop.py. Default capacity 3,
overridable via the V2_MAX_PARALLEL environment variable. The pipeline engine
in v1 (task_queue.py, PARALLEL_MAX_WORKERS=4) stays independent — it runs
inside run_pipeline subtasks and has its own lifecycle / failure model. This
gate only covers v2's direct-spawn tools (run_task, run_loop, run_loop_v2).

Usage
-----
    from parallel import CLI_GATE, acquire_slot, release_slot, gate_stats

    # Context manager form (preferred for simple wrappers):
    with CLI_GATE.slot():
        subprocess.run([...])

    # Manual form when status fields need to see 'queued' vs 'running':
    if CLI_GATE.try_acquire():
        try:
            task['status'] = 'running'
            subprocess.run([...])
        finally:
            CLI_GATE.release()
    else:
        task['status'] = 'queued'
        CLI_GATE.acquire()     # blocks until a slot frees
        try:
            task['status'] = 'running'
            subprocess.run([...])
        finally:
            CLI_GATE.release()

Status queries
--------------
gate_stats() returns {capacity, in_use, waiting_approx} for task_status / UI.
'waiting_approx' is best-effort: Python's Semaphore does not expose a
wait-queue length, so the module tracks increment/decrement around acquire()
calls instead.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager


def _read_capacity() -> int:
    raw = os.environ.get('V2_MAX_PARALLEL', '').strip()
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return 3


class ConcurrencyGate:
    """BoundedSemaphore wrapper that also tracks in-use and waiting counts."""

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError('capacity must be >= 1')
        self.capacity = capacity
        self._sem = threading.BoundedSemaphore(capacity)
        self._stat_lock = threading.Lock()
        self._in_use = 0
        self._waiting = 0

    # Acquire / release -----------------------------------------------------
    def try_acquire(self) -> bool:
        got = self._sem.acquire(blocking=False)
        if got:
            with self._stat_lock:
                self._in_use += 1
        return got

    def acquire(self) -> None:
        with self._stat_lock:
            self._waiting += 1
        try:
            self._sem.acquire()
        finally:
            with self._stat_lock:
                self._waiting -= 1
                self._in_use += 1

    def release(self) -> None:
        with self._stat_lock:
            if self._in_use <= 0:
                # Defensive: never double-release below zero.
                return
            self._in_use -= 1
        self._sem.release()

    @contextmanager
    def slot(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()

    # Introspection ---------------------------------------------------------
    def stats(self) -> dict:
        with self._stat_lock:
            return {
                'capacity': self.capacity,
                'in_use': self._in_use,
                'waiting': self._waiting,
            }


CLI_GATE = ConcurrencyGate(_read_capacity())


def acquire_slot() -> None:
    CLI_GATE.acquire()


def release_slot() -> None:
    CLI_GATE.release()


def gate_stats() -> dict:
    return CLI_GATE.stats()
