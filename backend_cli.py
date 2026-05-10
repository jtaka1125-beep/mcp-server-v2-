"""Claude Code CLI backend — subprocess-based, current production backend.

Wraps the same claude.EXE invocation that tools/task.py does, but behind the
Backend interface so the dispatcher can treat it interchangeably with other
backends in the future.

Thread safety: each run() call uses its own subprocess; there is no shared
mutable state. Multiple worker threads can call run() concurrently.
"""
from __future__ import annotations

import os
import subprocess
import time

from backend import Backend, Job, Result
from boundary import verify_claude_output  # [2026-04-26 C3] layer 1 verifier


class CliBackend(Backend):
    name = 'cli'

    def __init__(self, claude_exe: str):
        self._claude_exe = claude_exe

    def healthcheck(self) -> bool:
        return os.path.exists(self._claude_exe)

    def _build_env(self) -> dict:
        env = os.environ.copy()
        env.update({
            'USERPROFILE':       r'C:\Users\jun',
            'APPDATA':           r'C:\Users\jun\AppData\Roaming',
            'LOCALAPPDATA':      r'C:\Users\jun\AppData\Local',
            'HOMEDRIVE':         'C:',
            'HOMEPATH':          r'\Users\jun',
            'CLAUDE_CONFIG_DIR': r'C:\Users\jun\.claude',
        })
        return env

    def _build_cmd(self, job: Job) -> list:
        cmd = [self._claude_exe,
               '--dangerously-skip-permissions',
               '--print',
               job.prompt]
        if job.model and job.model not in ('gemini', None):
            cmd += ['--model', job.model]
        return cmd

    def run(self, job: Job) -> Result:
        if not self.healthcheck():
            return Result(
                job_id=job.job_id, ok=False,
                output='', exit_code=-1,
                error=f'claude.EXE not found: {self._claude_exe}',
                backend=self.name,
            )

        start = time.monotonic()
        try:
            proc = subprocess.run(
                self._build_cmd(job),
                capture_output=True, text=True,
                timeout=job.timeout_sec,
                env=self._build_env(), cwd=job.cwd,
                encoding='utf-8', errors='replace',
            )
            elapsed = time.monotonic() - start
            output = proc.stdout or ''
            if proc.stderr:
                output += f'\n[stderr]: {proc.stderr[:500]}'
            output += f'\n[exit_code]: {proc.returncode}'

            # [2026-04-26 C3] layer 1 boundary verification (entry 5ecbed0b)
            ok_l1, anomaly_reason = verify_claude_output(
                proc.returncode, proc.stdout or '', proc.stderr or ''
            )
            final_ok = (proc.returncode == 0) and ok_l1
            if not ok_l1:
                output += f'\n[layer1_anomaly]: {anomaly_reason}'

            return Result(
                job_id=job.job_id,
                ok=final_ok,
                output=output,
                exit_code=proc.returncode,
                error=(anomaly_reason if not ok_l1 else None),
                elapsed_sec=elapsed,
                backend=self.name,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return Result(
                job_id=job.job_id, ok=False,
                output='', exit_code=-1,
                error=f'timeout after {job.timeout_sec}s',
                elapsed_sec=elapsed,
                backend=self.name,
            )
        except Exception as e:
            elapsed = time.monotonic() - start
            return Result(
                job_id=job.job_id, ok=False,
                output='', exit_code=-2,
                error=f'{type(e).__name__}: {e}',
                elapsed_sec=elapsed,
                backend=self.name,
            )
