"""Dispatcher-backed drop-in for tools/loop.py.

Stage 3 precursor. Loops are bounded by the same CLI_GATE, but each loop
job calls the v1 loop_engine_v2.run_loop_v2 which itself may fan out
multiple CLI subprocesses. The dispatcher slot represents the outer loop;
inner CLI calls still go through CLI_GATE through whatever path loop_engine_v2
uses (direct subprocess for now).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import Backend, Job, Result
from dispatcher import Dispatcher, get_default
from parallel import gate_stats


# ---------------------------------------------------------------------------
# LoopBackend: a Backend that calls run_loop_v2 directly instead of spawning
# a CLI subprocess. This lets dispatcher's gate/status machinery bound the
# number of simultaneous loop jobs without duplicating the wrapping pattern.
# ---------------------------------------------------------------------------
class LoopBackend(Backend):
    name = 'loop'

    def run(self, job: Job) -> Result:
        max_rounds = int(job.extras.get('max_rounds', 3) or 3)
        engine = job.extras.get('engine')
        start = time.monotonic()
        try:
            from loop_engine_v2 import run_loop_v2  # v1 side
            r = run_loop_v2(job.prompt, max_rounds=max_rounds, engine=engine)
            elapsed = time.monotonic() - start
            if r is None:
                return Result(
                    job_id=job.job_id, ok=False, output='',
                    error='run_loop_v2 returned None',
                    elapsed_sec=elapsed, backend=self.name,
                )
            # run_loop_v2 returns a dict; serialise for output
            status = r.get('status', 'done')
            ok = status not in ('error',)
            return Result(
                job_id=job.job_id, ok=ok,
                output=json.dumps(r, ensure_ascii=False, indent=2),
                exit_code=0 if ok else 1,
                elapsed_sec=elapsed,
                backend=self.name,
                extras={'engine': engine, 'status': status},
            )
        except Exception as e:
            elapsed = time.monotonic() - start
            return Result(
                job_id=job.job_id, ok=False, output='',
                error=f'{type(e).__name__}: {e}',
                elapsed_sec=elapsed, backend=self.name,
            )


# A dedicated dispatcher for loops so they don't crowd out plain tasks.
# Gate is shared so total CLI concurrency remains bounded globally.
_loop_dispatcher = None
_registry: dict = {}


def _get_loop_dispatcher() -> Dispatcher:
    global _loop_dispatcher
    if _loop_dispatcher is None:
        _loop_dispatcher = Dispatcher(LoopBackend())
    return _loop_dispatcher


def _register(job_id: str, task: str, engine: str, started_at: float):
    _registry[job_id] = {
        'task': task[:200],
        'engine': engine,
        'started_at': started_at,
    }


# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------
def tool_run_loop(args: dict) -> str:
    task       = (args or {}).get('task', '')
    max_rounds = int((args or {}).get('max_rounds', 3) or 3)
    if not task:
        return 'ERROR: task is required'

    try:
        from loop_engine_v2 import classify_task
        engine = classify_task(task)
    except Exception:
        engine = 'code'

    job_id = str(uuid.uuid4())[:8]
    job = Job(
        job_id=job_id, kind='loop',
        prompt=task, cwd='.', timeout_sec=1200,
        extras={'max_rounds': max_rounds, 'engine': engine},
    )
    _register(job_id, task, engine, time.time())
    _get_loop_dispatcher().submit(job)
    return json.dumps({
        'job_id': job_id,
        'engine': engine,
        'max_rounds': max_rounds,
        'version': 'v2-dispatcher',
        'status': 'submitted',
    }, ensure_ascii=False)


def tool_run_loop_v2(args: dict) -> str:
    # Same as tool_run_loop but force engine=v2 path (classify_task already
    # returns the right engine; kept for schema compat).
    return tool_run_loop(args)


# ---------------------------------------------------------------------------
# loop_status
# ---------------------------------------------------------------------------
def tool_loop_status(args: dict) -> str:
    job_id = (args or {}).get('job_id', '')
    disp = _get_loop_dispatcher()
    gs = gate_stats()

    if not job_id:
        items = sorted(_registry.items(),
                       key=lambda kv: kv[1]['started_at'],
                       reverse=True)[:10]
        header = (
            f'=== Loop Jobs (dispatcher, gate: cap={gs["capacity"]} '
            f'in_use={gs["in_use"]} waiting={gs["waiting"]}) ==='
        )
        if not items:
            return header + '\nNo loop jobs found'
        lines = [header]
        for jid, meta in items:
            st = disp.status(jid) or '?'
            elapsed = time.time() - meta['started_at']
            lines.append(
                f"[{jid}] {st} ({elapsed:.0f}s) engine={meta['engine']} "
                f"- {meta['task'][:60]}")
        return '\n'.join(lines)

    st = disp.status(job_id)
    if not st:
        return f'ERROR: Loop job {job_id} not found'
    r = disp.result(job_id)
    meta = _registry.get(job_id, {'started_at': time.time(), 'task': '', 'engine': '?'})
    elapsed = time.time() - meta['started_at']
    result_text = r.output if r else '(running...)'
    return (
        f"=== Loop Job {job_id} ===\n"
        f"Status: {st}\n"
        f"Gate: cap={gs['capacity']} in_use={gs['in_use']} waiting={gs['waiting']}\n"
        f"Engine: {meta['engine']}\n"
        f"Elapsed: {elapsed:.0f}s\n"
        f"Task: {meta['task']}\n\n"
        f"--- Result ---\n{result_text[:3000]}"
    )


# ---------------------------------------------------------------------------
TOOLS = {
    'run_loop': {
        'description': 'Run autonomous loop: discuss->execute->review->verify (dispatcher-backed).',
        'schema': {'type': 'object', 'properties': {
            'task':       {'type': 'string'},
            'max_rounds': {'type': 'integer'},
        }, 'required': ['task']},
        'handler': tool_run_loop,
    },
    'run_loop_v2': {
        'description': 'Run loop engine v2 (3-engine: code/device/docs), dispatcher-backed.',
        'schema': {'type': 'object', 'properties': {
            'task':       {'type': 'string'},
            'max_rounds': {'type': 'integer'},
        }, 'required': ['task']},
        'handler': tool_run_loop_v2,
    },
    'loop_status': {
        'description': 'Check status of a loop engine job (dispatcher-backed).',
        'schema': {'type': 'object', 'properties': {
            'job_id': {'type': 'string'},
        }},
        'handler': tool_loop_status,
    },
}
