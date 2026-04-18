"""Dispatcher-backed drop-in for tools/task.py.

Stage 3 precursor: this module exposes the same TOOLS dict (run_task,
task_status, task_cancel) but the work goes through dispatcher.get_default()
instead of an ad-hoc subprocess + daemon thread. Left as a separate file so
the old tools/task.py stays running in production until V2_USE_DISPATCHER=1
is set in server.py's env.

Key differences from tools/task.py:

- No _tasks dict of our own. dispatcher is the single source of truth for
  job status and output.
- No threading.Thread per task. dispatcher has its own ThreadPoolExecutor.
- run_task returns almost immediately in async mode (after dispatcher.submit),
  sync mode loops on dispatcher.status until 'done'.
- task_status reads from dispatcher.status() and dispatcher.result().
- task_cancel calls dispatcher.cancel() which only works before a job starts.

Status values surfaced to callers:
    queued    - submitted but no worker has picked it up yet
    running   - a worker is executing the backend
    done      - result available (inspect ok / output for success)
    cancelled - cancel() arrived before the worker started
"""
from __future__ import annotations

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import MIRAGE_DIR

from backend import Job
from dispatcher import get_default
from parallel import gate_stats


# ---------------------------------------------------------------------------
# Server log tail helper (shared behavior with tools/task.py)
# ---------------------------------------------------------------------------
def _get_server_log_tail(n: int = 20) -> str:
    import os as _os
    log_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(__file__)), 'logs', 'server.log')
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return ''.join(lines[-n:]).strip()
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Job registry — remembers prompts and submit times for status display
# ---------------------------------------------------------------------------
# dispatcher stores status/result but not the original prompt / started_at.
# Keep a tiny side-table for pretty status output.
_registry: dict = {}


def _register(job_id: str, prompt: str, started_at: float):
    _registry[job_id] = {
        'prompt': prompt[:200],
        'started_at': started_at,
    }


# ---------------------------------------------------------------------------
# run_task
# ---------------------------------------------------------------------------
def tool_run_task(args: dict) -> str:
    prompt     = (args or {}).get('prompt', '')
    cwd        = (args or {}).get('cwd', MIRAGE_DIR)
    async_mode = (args or {}).get('async', True)
    model      = (args or {}).get('model', None)

    if not prompt:
        return 'ERROR: prompt is required'

    job_id = str(uuid.uuid4())[:8]
    job = Job(
        job_id=job_id, kind='task',
        prompt=prompt, cwd=cwd,
        model=model, timeout_sec=300,
    )
    _register(job_id, prompt, time.time())
    disp = get_default()
    disp.submit(job)

    if async_mode:
        return f'Task started: {job_id}\nPrompt: {prompt[:120]}\nUse task_status to check.'

    # Sync: block until done with a sane upper bound
    deadline = time.monotonic() + 320.0
    while time.monotonic() < deadline:
        if disp.status(job_id) == 'done':
            r = disp.result(job_id)
            if r and r.ok:
                return r.output
            tail = _get_server_log_tail(20)
            msg = (r.error if (r and r.error) else 'no output')
            if r and r.output:
                msg = r.output
            if tail:
                msg += f'\n\n--- Server Log (last 20 lines) ---\n{tail}'
            return f'ERROR: {msg}'
        time.sleep(0.2)
    return f'ERROR: sync wait exceeded 320s for {job_id}'


# ---------------------------------------------------------------------------
# task_status
# ---------------------------------------------------------------------------
def tool_task_status(args: dict) -> str:
    task_id = (args or {}).get('task_id', '')
    disp = get_default()
    gs = gate_stats()

    if not task_id:
        # List recent tasks. Sort by submit time from the registry side table.
        items = sorted(_registry.items(),
                       key=lambda kv: kv[1]['started_at'],
                       reverse=True)[:10]
        header = (
            f'=== Tasks (dispatcher, gate: cap={gs["capacity"]} '
            f'in_use={gs["in_use"]} waiting={gs["waiting"]}) ==='
        )
        if not items:
            return header + '\nNo tasks found'
        lines = [header]
        for tid, meta in items:
            st = disp.status(tid) or '?'
            elapsed = time.time() - meta['started_at']
            lines.append(
                f"[{tid}] {st} ({elapsed:.0f}s) - {meta['prompt'][:60]}...")
        return '\n'.join(lines)

    st = disp.status(task_id)
    if not st:
        return f'ERROR: task {task_id} not found'
    r = disp.result(task_id)
    meta = _registry.get(task_id, {'started_at': time.time(), 'prompt': ''})
    elapsed = time.time() - meta['started_at']
    output = r.output if r else '(pending...)'
    if r and not r.ok and r.error:
        output = f'{output}\n[error]: {r.error}'
    return (
        f"=== Task {task_id} ===\n"
        f"Status: {st}\n"
        f"Gate: cap={gs['capacity']} in_use={gs['in_use']} waiting={gs['waiting']}\n"
        f"Elapsed: {elapsed:.0f}s\n"
        f"Prompt: {meta['prompt']}\n\n"
        f"--- Output ---\n{output}"
    )


# ---------------------------------------------------------------------------
# task_cancel
# ---------------------------------------------------------------------------
def tool_task_cancel(args: dict) -> str:
    task_id = (args or {}).get('task_id', '')
    if not task_id:
        return 'ERROR: task_id is required'
    disp = get_default()
    st = disp.status(task_id)
    if not st:
        return f'ERROR: task {task_id} not found'
    if st != 'queued':
        return f'ERROR: task {task_id} is {st}, only queued tasks can be cancelled'
    if disp.cancel(task_id):
        return f'Task {task_id} cancelled'
    return f'ERROR: failed to cancel {task_id}'


# ---------------------------------------------------------------------------
TOOLS = {
    'run_task': {
        'description': 'Run a complex task using Claude Code CLI (dispatcher-backed).',
        'schema': {'type': 'object', 'properties': {
            'prompt':  {'type': 'string'},
            'cwd':     {'type': 'string'},
            'async':   {'type': 'boolean'},
            'model':   {'type': 'string'},
        }, 'required': ['prompt']},
        'handler': tool_run_task,
    },
    'task_status': {
        'description': 'Check status of a running or completed task (dispatcher-backed).',
        'schema': {'type': 'object', 'properties': {
            'task_id': {'type': 'string'},
        }},
        'handler': tool_task_status,
    },
    'task_cancel': {
        'description': 'Cancel a queued task before a worker picks it up (dispatcher-backed).',
        'schema': {'type': 'object', 'properties': {
            'task_id': {'type': 'string'},
        }, 'required': ['task_id']},
        'handler': tool_task_cancel,
    },
}
