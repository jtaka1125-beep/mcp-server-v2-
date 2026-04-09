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

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# タスク管理ストレージ
# ---------------------------------------------------------------------------
_tasks: dict = {}
_tasks_lock = threading.Lock()

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
        with _tasks_lock:
            _tasks[task_id]['status'] = 'running'

        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300, env=env, cwd=cwd,
            encoding='utf-8', errors='replace',
        )
        output = result.stdout
        if result.stderr:
            output += f'\n[stderr]: {result.stderr[:500]}'
        output += f'\n[exit_code]: {result.returncode}'

        with _tasks_lock:
            _tasks[task_id]['status'] = 'completed'
            _tasks[task_id]['output'] = output

    except subprocess.TimeoutExpired:
        with _tasks_lock:
            _tasks[task_id]['status'] = 'timeout'
            _tasks[task_id]['output'] = 'ERROR: timeout after 300s'
    except Exception as e:
        with _tasks_lock:
            _tasks[task_id]['status'] = 'error'
            _tasks[task_id]['error'] = str(e)

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
            if not _tasks:
                return 'No tasks found'
            lines = ['=== Tasks ===']
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
