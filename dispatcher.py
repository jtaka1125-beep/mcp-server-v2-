"""Dispatcher — job intake, worker pool, result storage.

Sits between tools/*.py (producers) and backend_*.py (executors). A single
in-process dispatcher instance serves all tools. Workers are threads from
concurrent.futures.ThreadPoolExecutor, bounded by CLI_GATE's capacity so
the underlying CLI never runs more than N jobs at once.

State model:
    queued   - submitted, waiting for a worker
    running  - worker has started executing
    done     - Result stored, success or not
    cancelled - submit_cancel(job_id) was called before a worker picked it up

Consumer pattern:
    dispatcher.submit(job)
    # ... later ...
    status = dispatcher.status(job_id)
    result = dispatcher.result(job_id)   # None until status=done

This is stage 2 of the P4-2 plan. tools/*.py still call subprocess directly
in stage 1; stage 3 will switch them to dispatcher.submit().
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Dict, Optional

from backend import Backend, Job, Result
from parallel import CLI_GATE

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, backend: Backend, max_workers: Optional[int] = None):
        self._backend = backend
        # Default worker count follows the gate capacity. A bigger pool is
        # pointless because CLI_GATE will just block extra workers.
        workers = max_workers if max_workers is not None else CLI_GATE.capacity
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix='disp-worker')
        self._lock = threading.Lock()
        self._status: Dict[str, str] = {}             # job_id -> status
        self._results: Dict[str, Result] = {}         # job_id -> Result
        self._futures: Dict[str, Future] = {}         # job_id -> Future
        self._submitted_at: Dict[str, float] = {}     # job_id -> monotonic
        self._cancelled: set = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def submit(self, job: Job) -> str:
        """Enqueue job. Returns job_id. Idempotent on duplicate job_id."""
        with self._lock:
            if job.job_id in self._status:
                return job.job_id
            self._status[job.job_id] = 'queued'
            self._submitted_at[job.job_id] = time.monotonic()

        fut = self._pool.submit(self._run_wrapped, job)
        with self._lock:
            self._futures[job.job_id] = fut
        fut.add_done_callback(lambda f, jid=job.job_id: self._on_done(jid, f))
        return job.job_id

    def status(self, job_id: str) -> Optional[str]:
        with self._lock:
            return self._status.get(job_id)

    def result(self, job_id: str) -> Optional[Result]:
        with self._lock:
            return self._results.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Best-effort cancel. Returns True if the job was still queued and got
        cancelled before start. Running jobs cannot be interrupted from here.
        """
        with self._lock:
            fut = self._futures.get(job_id)
            if not fut:
                return False
            if self._status.get(job_id) != 'queued':
                return False
            # Future.cancel() only works if the task has not started yet
            cancelled = fut.cancel()
            if cancelled:
                self._status[job_id] = 'cancelled'
                self._cancelled.add(job_id)
            return cancelled

    def stats(self) -> dict:
        with self._lock:
            counts = {'queued': 0, 'running': 0, 'done': 0, 'cancelled': 0}
            for s in self._status.values():
                counts[s] = counts.get(s, 0) + 1
            return {
                'backend': self._backend.name,
                'gate_capacity': CLI_GATE.capacity,
                'counts': counts,
                'total': len(self._status),
            }

    def shutdown(self, wait: bool = True, timeout: Optional[float] = None):
        """Stop accepting new jobs; optionally wait for in-flight to finish."""
        self._pool.shutdown(wait=wait, cancel_futures=not wait)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _run_wrapped(self, job: Job) -> Result:
        # Respect CLI_GATE: workers can outnumber gate slots if a caller
        # constructed Dispatcher with max_workers > capacity. Gate acquisition
        # transitions status to 'running'.
        CLI_GATE.acquire()
        try:
            with self._lock:
                if job.job_id in self._cancelled:
                    return Result(
                        job_id=job.job_id, ok=False,
                        output='', exit_code=-1,
                        error='cancelled before start',
                        backend=self._backend.name,
                    )
                self._status[job.job_id] = 'running'
            try:
                return self._backend.run(job)
            except Exception as e:
                log.exception('backend %s raised on job %s', self._backend.name, job.job_id)
                return Result(
                    job_id=job.job_id, ok=False,
                    output='', exit_code=-2,
                    error=f'backend raised: {type(e).__name__}: {e}',
                    backend=self._backend.name,
                )
        finally:
            CLI_GATE.release()

    def _on_done(self, job_id: str, fut: Future):
        try:
            r = fut.result()
        except Exception as e:
            # Future cancelled or unexpected error — synthesise a Result
            r = Result(
                job_id=job_id, ok=False,
                output='', exit_code=-3,
                error=f'future error: {type(e).__name__}: {e}',
                backend=self._backend.name,
            )
        with self._lock:
            self._results[job_id] = r
            # Keep 'cancelled' status if already set
            if self._status.get(job_id) != 'cancelled':
                self._status[job_id] = 'done'


# Module-level singleton, constructed lazily so tests can swap the backend.
_default: Optional[Dispatcher] = None
_default_lock = threading.Lock()


def get_default() -> Dispatcher:
    global _default
    with _default_lock:
        if _default is None:
            # Stage 2: wire the CLI backend, same executable path task.py uses.
            from config import CLAUDE_EXE
            from backend_cli import CliBackend
            _default = Dispatcher(CliBackend(CLAUDE_EXE))
        return _default


def reset_default(new: Optional[Dispatcher] = None):
    """Test helper: replace or clear the singleton."""
    global _default
    with _default_lock:
        if _default is not None and new is not _default:
            try:
                _default.shutdown(wait=False)
            except Exception:
                pass
        _default = new
