"""Mock backend for dispatcher unit tests.

Returns a deterministic Result without doing any I/O. Useful for:
- Exercising dispatcher queue/worker semantics without firing claude.EXE
- Simulating timeouts, failures, and variable runtimes to stress-test
  the dispatcher's error handling
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from backend import Backend, Job, Result


class MockBackend(Backend):
    name = 'mock'

    def __init__(
        self,
        delay_sec: float = 0.0,
        ok: bool = True,
        output: str = 'mock: ok',
        raise_timeout: bool = False,
        side_effect: Optional[Callable[[Job], None]] = None,
    ):
        self._delay = delay_sec
        self._ok = ok
        self._output = output
        self._raise_timeout = raise_timeout
        self._side_effect = side_effect

    def run(self, job: Job) -> Result:
        if self._side_effect:
            self._side_effect(job)

        if self._raise_timeout and self._delay > job.timeout_sec:
            # Simulate a timeout: block until timeout, then report failure
            time.sleep(job.timeout_sec)
            return Result(
                job_id=job.job_id, ok=False,
                output='', exit_code=-1,
                error=f'mock timeout after {job.timeout_sec}s',
                elapsed_sec=float(job.timeout_sec),
                backend=self.name,
            )

        if self._delay > 0:
            time.sleep(self._delay)

        return Result(
            job_id=job.job_id,
            ok=self._ok,
            output=self._output,
            exit_code=0 if self._ok else 1,
            elapsed_sec=self._delay,
            backend=self.name,
        )
