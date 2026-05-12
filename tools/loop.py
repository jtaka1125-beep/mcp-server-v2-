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
# ---------------------------------------------------------------------------
# run_dreaming
# ---------------------------------------------------------------------------
def _run_dreaming_job(job_id: str, namespaces: list, auto_compact: bool):
    """Dreaming job: memory consolidation after session end."""
    import urllib.request

    def mcp_call(name, args={}):
        body = json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': args}
        }).encode()
        req = urllib.request.Request(
            'http://127.0.0.1:3001/mcp', data=body,
            headers={'Content-Type': 'application/json'}
        )
        res = urllib.request.urlopen(req, timeout=180)
        raw = json.loads(res.read())
        content = raw.get('result', {}).get('content', [{}])
        text = content[0].get('text', '{}') if content else '{}'
        try:
            return json.loads(text)
        except Exception:
            return {'raw': text}

    def cerebras_summarize(entries_text: str, namespace: str) -> str:
        """qwen-3-235b で今日の学びを要約する"""
        import urllib.request as ur
        payload = {
            'model': 'qwen-3-235b-a22b-instruct-2507',
            'max_tokens': 400,
            'messages': [{
                'role': 'user',
                'content': (
                    f'MirageSystem の {namespace} namespace の本日の decision エントリ:\n\n'
                    f'{entries_text}\n\n'
                    '上記から「今日学んだこと」を 3-5 行の日本語で簡潔に要約してください。'
                    '設計判断・バグ修正・観察事項の重要なものだけを抽出してください。'
                    '前置きや後書きは不要、箇条書きで。'
                )
            }]
        }
        req = ur.Request(
            'https://api.cerebras.ai/v1/chat/completions',
            data=json.dumps(payload).encode(),
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'MirageSystem/1.0',
                'Authorization': f'Bearer {_get_cerebras_key()}',
            }
        )
        try:
            res = ur.urlopen(req, timeout=60)
            r = json.loads(res.read())
            return r['choices'][0]['message']['content'].strip()
        except Exception as e:
            return f'(要約失敗: {e})'

    def _get_cerebras_key() -> str:
        try:
            import os
            key = os.environ.get('CEREBRAS_API_KEY', '')
            if key:
                return key
            cfg_path = r'C:\MirageWork\mcp-server-v2\config.json'
            with open(cfg_path, encoding='utf-8') as f:
                cfg = json.load(f)
            return cfg.get('cerebras_api_key', '')
        except Exception:
            return ''

    steps = []
    errors = []

    try:
        with _jobs_lock:
            _loop_jobs[job_id]['steps'] = steps

        for ns in namespaces:
            steps.append(f'[{ns}] Step 1: 今日の activity 取得')
            activity = mcp_call('memory_recent_activity', {'days': 1, 'detail': True})
            new_entries = activity.get('new_entries', 0)
            steps.append(f'[{ns}] new_entries={new_entries}')

            if new_entries == 0:
                steps.append(f'[{ns}] 新規エントリなし、スキップ')
                continue

            # Step 2: 最新 decision を検索して要約
            steps.append(f'[{ns}] Step 2: 今日の decision 検索')
            search = mcp_call('memory_search', {
                'query': 'decision', 'namespace': ns,
                'limit': 20, 'types': ['decision']
            })
            entries = search.get('entries', [])
            if not entries:
                steps.append(f'[{ns}] decision エントリなし、スキップ')
                continue

            # 今日分だけ絞る (age_min < 1440 = 24h)
            today = [e for e in entries if e.get('age_min', 9999) < 1440]
            if not today:
                steps.append(f'[{ns}] 今日の decision なし')
                continue

            entries_text = '\n'.join([
                f"- {e.get('title','?')}: {e.get('content','')[:100]}"
                for e in today[:10]
            ])

            steps.append(f'[{ns}] Step 3: qwen-3-235b で要約')
            summary = cerebras_summarize(entries_text, ns)
            steps.append(f'[{ns}] summary={summary[:80]}...')

            # Step 4: dreaming_summary として記録
            steps.append(f'[{ns}] Step 4: dreaming_summary 記録')
            mcp_call('memory_append_decision', {
                'namespace': ns,
                'title': f'Dreaming summary ({__import__("datetime").date.today()})',
                'content': summary,
                'importance': 3,
                'tags': ['dreaming', 'daily_summary'],
            })
            steps.append(f'[{ns}] Step 4 完了')

            # Step 5: compact (auto_compact=True かつ stale 確認)
            if auto_compact:
                steps.append(f'[{ns}] Step 5: compact 確認')
                maint = mcp_call('memory_maintenance', {
                    'namespace': ns, 'dry_run': True, 'allow_auto': False
                })
                if maint.get('compact_recommended'):
                    steps.append(f'[{ns}] compact 実行')
                    mcp_call('memory_compact', {'namespace': ns})
                    steps.append(f'[{ns}] compact 完了')
                else:
                    steps.append(f'[{ns}] compact 不要')

        # Step 6: link promotion (全 namespace 共通)
        steps.append('Step 6: link_promotion_candidates 確認')
        candidates = mcp_call('memory_link_promotion_candidates', {'limit': 20})
        n_candidates = len(candidates.get('candidates', []))
        steps.append(f'candidates={n_candidates}')
        if n_candidates > 0:
            steps.append('link promotion 実行')
            mcp_call('memory_link_bulk_promote', {'min_score': 0.95, 'limit': 20})
            steps.append('link promotion 完了')

        with _jobs_lock:
            _loop_jobs[job_id].update({
                'status': 'done',
                'result': {'steps': steps, 'errors': errors},
                'finished_at': time.time(),
            })

    except Exception as e:
        errors.append(str(e))
        with _jobs_lock:
            _loop_jobs[job_id].update({
                'status': 'error',
                'result': {'steps': steps, 'errors': errors},
                'finished_at': time.time(),
            })


def tool_run_dreaming(args: dict) -> str:
    """セッション終了後の外部記憶 Dreaming を非同期実行"""
    namespaces = (args or {}).get(
        'namespaces',
        ['mirage-vulkan', 'mirage-android', 'mirage-infra', 'mirage-design']
    )
    auto_compact = bool((args or {}).get('auto_compact', True))

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _loop_jobs[job_id] = {
            'status': 'running',
            'task': f'dreaming namespaces={namespaces}',
            'engine': 'dreaming',
            'result': None,
            'steps': [],
            'started_at': time.time(),
            'version': 'dreaming-v1',
        }

    threading.Thread(
        target=_run_dreaming_job,
        args=(job_id, namespaces, auto_compact),
        daemon=True,
    ).start()

    return json.dumps({
        'job_id': job_id,
        'status': 'running',
        'namespaces': namespaces,
        'auto_compact': auto_compact,
        'message': 'Dreaming started. Use loop_status to check progress.',
    }, ensure_ascii=False)


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
    'run_dreaming': {
        'description': 'Post-session memory consolidation (Dreaming). Summarizes today decisions, records dreaming_summary, optional compact + link promotion. Runs async via qwen-3-235b (Cerebras free tier).',
        'schema': {'type': 'object', 'properties': {
            'namespaces': {
                'type': 'array', 'items': {'type': 'string'},
                'description': 'Namespaces to process (default: all 4)',
            },
            'auto_compact': {
                'type': 'boolean',
                'description': 'Run compact if recommended (default: true)',
            },
        }},
        'handler': tool_run_dreaming,
    },
    'loop_status': {
        'description': 'Check status of a loop engine job.',
        'schema': {'type': 'object', 'properties': {
            'job_id': {'type': 'string'},
        }},
        'handler': tool_loop_status,
    },
}
