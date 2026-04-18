"""Backend interface for v2 dispatcher.

A backend is anything that can execute a job and return a Result. The
current production backend is backend_cli (claude.EXE subprocess). Future
backends can replace CLI without touching server.py or tools/*.

All backends MUST be thread-safe: the dispatcher calls run() from worker
threads in parallel.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Job:
    """A unit of work handed from tools to the dispatcher."""
    job_id: str
    kind: str              # 'task' | 'loop' | future kinds
    prompt: str
    cwd: str
    model: Optional[str] = None
    timeout_sec: int = 300
    # Extra kind-specific args (loop's max_rounds, engine, etc.)
    extras: dict = field(default_factory=dict)


@dataclass
class Result:
    """Normalised backend result."""
    job_id: str
    ok: bool               # True iff the backend considers the run successful
    output: str            # Stdout + any attached context (log tail, etc.)
    exit_code: int = 0     # 0 on success; backend-specific otherwise
    error: Optional[str] = None
    elapsed_sec: float = 0.0
    # Metadata for observability
    backend: str = ''      # 'cli' | 'api' | 'mock'
    extras: dict = field(default_factory=dict)


class Backend(abc.ABC):
    """Execution backend contract."""

    name: str = 'abstract'

    @abc.abstractmethod
    def run(self, job: Job) -> Result:
        """Execute job synchronously. MUST be thread-safe.

        Implementations should respect job.timeout_sec and return a Result
        with ok=False (not raise) on timeout or recoverable errors. Raise
        only on programming errors.
        """
        raise NotImplementedError

    def healthcheck(self) -> bool:
        """Optional: quick probe that the backend is usable.

        Default returns True; override in backends that have prerequisites
        (e.g. CLI binary exists, API key set, etc.).
        """
        return True
