"""
tools/task.py - タスク実行系ツール
=====================================
Claude Code CLI経由でタスクを非同期実行する。
既存のrun_claude_code_async / running_tasksのロジックを再実装。
"""
import os
import sys
import uuid
import time
import threading
import subprocess
import shutil
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import MIRAGE_DIR, CLAUDE_EXE
from parallel import CLI_GATE, gate_stats

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# タスク管理ストレージ
# ---------------------------------------------------------------------------
_tasks: dict = {}
_tasks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Server log tail helper (auto-attach on failure)
# ---------------------------------------------------------------------------
def _get_server_log_tail(n: int = 20) -> str:
    """Return last N lines of server.log, empty string on error."""
    import os as _os
    log_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), 'logs', 'server.log')
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return ''.join(lines[-n:]).strip()
    except Exception:
        return ''

# ---------------------------------------------------------------------------
# P4-2: gated subprocess.run wrapper (shared CLI concurrency gate)
# ---------------------------------------------------------------------------
def _gated_subprocess_run(task_id, cmd, **kwargs):
    """Block on CLI_GATE; mark task queued if no slot; run; release on exit."""
    if not CLI_GATE.try_acquire():
        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id]['status'] = 'queued'
        CLI_GATE.acquire()
    try:
        with _tasks_lock:
            if task_id in _tasks:
                _tasks[task_id]['status'] = 'running'
        return subprocess.run(cmd, **kwargs)
    finally:
        CLI_GATE.release()

# ---------------------------------------------------------------------------
# Claude Code CLI 実行
# ---------------------------------------------------------------------------
def _run_claude_async(task_id: str, prompt: str, cwd: str, model: str = None):
    env = os.environ.copy()
    env.update({
        'USERPROFILE':       r'C:\Users\jun',
        'APPDATA':           r'C:\Users\jun\AppData\Roaming',
        'LOCALAPPDATA':      r'C:\Users\jun\AppData\Local',
        'HOMEDRIVE':         'C:',
        'HOMEPATH':          r'\Users\jun',
        'CLAUDE_CONFIG_DIR': r'C:\Users\jun\.claude',
    })

    cmd = [CLAUDE_EXE, '--dangerously-skip-permissions', '--print', prompt]
    if model and model not in ('gemini', None):
        cmd += ['--model', model]

    try:
        result = _gated_subprocess_run(
            task_id, cmd,
            capture_output=True, text=True,
            timeout=300, env=env, cwd=cwd,
            encoding='utf-8', errors='replace',
        )
        output = result.stdout
        if result.stderr:
            output += f'\n[stderr]: {result.stderr[:500]}'
        output += f'\n[exit_code]: {result.returncode}'

        # Attach server log tail on non-zero exit
        if result.returncode != 0:
            log_tail = _get_server_log_tail(20)
            if log_tail:
                output += f'\n\n--- Server Log (last 20 lines) ---\n{log_tail}'
        with _tasks_lock:
            _tasks[task_id]['status'] = 'completed'
            _tasks[task_id]['output'] = output

    except subprocess.TimeoutExpired:
        log_tail = _get_server_log_tail(30)
        with _tasks_lock:
            _tasks[task_id]['status'] = 'timeout'
            _tasks[task_id]['output'] = (
                'ERROR: timeout after 300s'
                + (f'\n\n--- Server Log (last 30 lines) ---\n{log_tail}' if log_tail else '')
            )
    except Exception as e:
        log_tail = _get_server_log_tail(20)
        with _tasks_lock:
            _tasks[task_id]['status'] = 'error'
            _tasks[task_id]['error'] = str(e)
            _tasks[task_id]['output'] = (
                f'ERROR: {e}'
                + (f'\n\n--- Server Log (last 20 lines) ---\n{log_tail}' if log_tail else '')
            )

# ---------------------------------------------------------------------------
# run_task
# ---------------------------------------------------------------------------
def tool_run_task(args: dict) -> str:
    prompt    = (args or {}).get('prompt', '')
    cwd       = (args or {}).get('cwd', MIRAGE_DIR)
    async_mode = (args or {}).get('async', True)
    model     = (args or {}).get('model', None)

    if not prompt:
        return 'ERROR: prompt is required'

    task_id = str(uuid.uuid4())[:8]
    with _tasks_lock:
        _tasks[task_id] = {
            'prompt': prompt, 'cwd': cwd,
            'status': 'starting', 'output': '',
            'started_at': time.time(), 'model': model,
        }

    if async_mode:
        threading.Thread(
            target=_run_claude_async,
            args=(task_id, prompt, cwd, model),
            daemon=True,
        ).start()
        return f'Task started: {task_id}\nPrompt: {prompt}\nUse task_status to check.'
    else:
        _run_claude_async(task_id, prompt, cwd, model)
        with _tasks_lock:
            return _tasks[task_id].get('output', 'ERROR: no output')

# ---------------------------------------------------------------------------
# task_status
# ---------------------------------------------------------------------------
def tool_task_status(args: dict) -> str:
    task_id = (args or {}).get('task_id', '')

    with _tasks_lock:
        if not task_id:
            gs = gate_stats()
            header = (
                '=== Tasks (gate: cap=%d in_use=%d waiting=%d) ===' %
                (gs['capacity'], gs['in_use'], gs['waiting'])
            )
            if not _tasks:
                return header + '\nNo tasks found'
            lines = [header]
            for tid, task in list(_tasks.items())[-10:]:
                elapsed = time.time() - task.get('started_at', time.time())
                lines.append(
                    f"[{tid}] {task.get('status')} ({elapsed:.0f}s) "
                    f"- {task.get('prompt','')[:60]}..."
                )
            return '\n'.join(lines)

        task = _tasks.get(task_id)
        if not task:
            return f'ERROR: task {task_id} not found'

        elapsed = time.time() - task.get('started_at', time.time())
        output  = task.get('output', task.get('error', '(running...)'))
        return (
            f"=== Task {task_id} ===\n"
            f"Status: {task['status']}\n"
            f"Gate: cap={gate_stats()['capacity']} in_use={gate_stats()['in_use']} waiting={gate_stats()['waiting']}\n"
            f"Elapsed: {elapsed:.0f}s\n"
            f"Prompt: {task.get('prompt','')[:100]}\n\n"
            f"--- Output ---\n{output[-3000:]}"
        )

# ---------------------------------------------------------------------------
# task_cancel
# ---------------------------------------------------------------------------
def tool_task_cancel(args: dict) -> dict:
    task_id = (args or {}).get('task_id', '')
    if not task_id:
        return {'error': 'task_id required'}
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return {'error': f'task {task_id} not found'}
        task['status'] = 'cancelled'
    return {'ok': True, 'task_id': task_id, 'message': 'Task marked as cancelled'}

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
TOOLS = {
    'run_task': {
        'description': 'Run a complex task using Claude Code CLI.',
        'schema': {'type': 'object', 'properties': {
            'prompt':  {'type': 'string'},
            'cwd':     {'type': 'string'},
            'async':   {'type': 'boolean'},
            'model':   {'type': 'string'},
        }, 'required': ['prompt']},
        'handler': tool_run_task,
    },
    'task_status': {
        'description': 'Check status of a running or completed Claude Code task.',
        'schema': {'type': 'object', 'properties': {
            'task_id': {'type': 'string'},
        }},
        'handler': tool_task_status,
    },
    'task_cancel': {
        'description': 'Cancel a running Claude Code task.',
        'schema': {'type': 'object', 'properties': {
            'task_id': {'type': 'string'},
        }, 'required': ['task_id']},
        'handler': tool_task_cancel,
    },
}
