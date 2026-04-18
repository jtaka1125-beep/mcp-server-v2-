"""
tools/loop.py - ループエンジン系ツール
=========================================
loop_engine_v2.py を呼び出すシンプルなwrapper。
ロジックはloop_engine_v2.pyに任せる。
"""
import os
import sys
import uuid
import time
import json
import threading
import logging
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from parallel import CLI_GATE, gate_stats

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, r'C:\MirageWork\mcp-server')

log = logging.getLogger(__name__)

_loop_jobs: dict = {}
_jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# 内部: ジョブ実行
# ---------------------------------------------------------------------------
def _run_job(job_id: str, task: str, max_rounds: int, engine: str = None):
    try:
        from loop_engine_v2 import run_loop_v2
        result = run_loop_v2(task, max_rounds=max_rounds, engine=engine)
        with _jobs_lock:
            if result is None:
                result = {'status': 'error', 'error': 'run_loop_v2 returned None'}
            _loop_jobs[job_id]['status'] = result.get('status', 'done')
            _loop_jobs[job_id]['result'] = result
    except Exception as e:
        log.error(f'loop job {job_id} error: {e}', exc_info=True)  # traceback付き
        with _jobs_lock:
            _loop_jobs[job_id]['status'] = 'error'
            _loop_jobs[job_id]['result'] = {'error': str(e)}

def _run_job_gated(job_id: str, task: str, max_rounds: int, engine: str = None):
    """P4-2: gate-bounded wrapper around _run_job."""
    if not CLI_GATE.try_acquire():
        with _jobs_lock:
            if job_id in _loop_jobs:
                _loop_jobs[job_id]['status'] = 'queued'
        CLI_GATE.acquire()
    try:
        with _jobs_lock:
            if job_id in _loop_jobs:
                _loop_jobs[job_id]['status'] = 'running'
        _run_job(job_id, task, max_rounds, engine)
    finally:
        CLI_GATE.release()

# ---------------------------------------------------------------------------
# run_loop
# ---------------------------------------------------------------------------
def tool_run_loop(args: dict) -> str:
    task       = (args or {}).get('task', '')
    max_rounds = int((args or {}).get('max_rounds', 3) or 3)
    if not task:
        return 'ERROR: task is required'

    # 自動エンジン選択
    try:
        from loop_engine_v2 import classify_task
        engine = classify_task(task)
    except Exception:
        engine = 'code'

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _loop_jobs[job_id] = {
            'status': 'running', 'task': task,
            'engine': engine, 'result': None,
            'started_at': time.time(), 'version': 'v2',
        }

    threading.Thread(
        target=_run_job_gated,
        args=(job_id, task, max_rounds, engine),
        daemon=True,
    ).start()

    return json.dumps({
        'job_id': job_id, 'status': 'running',
        'engine': engine, 'task': task,
    }, ensure_ascii=False)

# ---------------------------------------------------------------------------
# run_loop_v2
# ---------------------------------------------------------------------------
def tool_run_loop_v2(args: dict) -> str:
    task       = (args or {}).get('task', '')
    max_rounds = int((args or {}).get('max_rounds', 3) or 3)
    engine     = (args or {}).get('engine', None)  # None = auto-detect
    if not task:
        return 'ERROR: task is required'

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _loop_jobs[job_id] = {
            'status': 'running', 'task': task,
            'engine': engine or 'auto',
            'result': None, 'started_at': time.time(),
            'version': 'v2',
        }

    threading.Thread(
        target=_run_job_gated,
        args=(job_id, task, max_rounds, engine),
        daemon=True,
    ).start()

    return json.dumps({
        'job_id': job_id, 'status': 'running',
        'engine': engine or 'auto', 'task': task,
    }, ensure_ascii=False)

# ---------------------------------------------------------------------------
# loop_status
# ---------------------------------------------------------------------------
def tool_loop_status(args: dict) -> str:
    job_id = (args or {}).get('job_id', '')

    with _jobs_lock:
        if not job_id:
            gs = gate_stats()
            header = (
                '=== Loop Jobs (gate: cap=%d in_use=%d waiting=%d) ===' %
                (gs['capacity'], gs['in_use'], gs['waiting'])
            )
            if not _loop_jobs:
                return header + '\nNo loop jobs found'
            lines = [header]
            for jid, job in list(_loop_jobs.items())[-10:]:
                elapsed = time.time() - job.get('started_at', time.time())
                lines.append(
                    f"[{jid}] {job['status']} ({elapsed:.0f}s) "
                    f"engine={job.get('engine','?')} - {job['task'][:60]}"
                )
            return '\n'.join(lines)

        job = _loop_jobs.get(job_id)
        if not job:
            return f'ERROR: Loop job {job_id} not found'

        elapsed = time.time() - job.get('started_at', time.time())
        result  = job.get('result')
        result_text = json.dumps(result, ensure_ascii=False, indent=2)[:3000] \
                      if result else '(running...)'

        return (
            f"=== Loop Job {job_id} ===\n"
            f"Status: {job['status']}\n"
            f"Gate: cap={gate_stats()['capacity']} in_use={gate_stats()['in_use']} waiting={gate_stats()['waiting']}\n"
            f"Engine: {job.get('engine','?')}\n"
            f"Elapsed: {elapsed:.0f}s\n"
            f"Task: {job['task']}\n\n"
            f"--- Result ---\n{result_text}"
        )

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
TOOLS = {
    'run_loop': {
        'description': 'Run autonomous loop: discuss→execute→review→verify.',
        'schema': {'type': 'object', 'properties': {
            'task':       {'type': 'string'},
            'max_rounds': {'type': 'integer'},
        }, 'required': ['task']},
        'handler': tool_run_loop,
    },
    'run_loop_v2': {
        'description': 'Run loop engine v2 (3-engine: code/device/docs).',
        'schema': {'type': 'object', 'properties': {
            'task':       {'type': 'string'},
            'max_rounds': {'type': 'integer'},
            'engine':     {'type': 'string',
                           'enum': ['code', 'device', 'docs']},
        }, 'required': ['task']},
        'handler': tool_run_loop_v2,
    },
    'loop_status': {
        'description': 'Check status of a loop engine job.',
        'schema': {'type': 'object', 'properties': {
            'job_id': {'type': 'string'},
        }},
        'handler': tool_loop_status,
    },
}
