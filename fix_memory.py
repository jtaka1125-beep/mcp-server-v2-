"""
tools/memory.py - memory系MCPツール（完全版）
"""
import sys
import os
import uuid
import threading
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import llm
from memory import store as mem
from memory import compact as mem_compact

log = logging.getLogger(__name__)
_jobs: dict = {}
_jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# memory_bootstrap
# ---------------------------------------------------------------------------
def tool_memory_bootstrap(args: dict) -> dict:
    ns = (args or {}).get('namespace', 'mirage-infra')
    max_chars = int((args or {}).get('max_chars', 800) or 800)
    return mem.get_bootstrap(ns, max_chars=max_chars)

# ---------------------------------------------------------------------------
# memory_compact
# ---------------------------------------------------------------------------
def tool_memory_compact(args: dict) -> dict:
    ns        = (args or {}).get('namespace', 'mirage-infra')
    max_chars = int((args or {}).get('max_chars', 800) or 800)
    window    = int((args or {}).get('window', 200) or 200)

    msgs = mem.fetch_recent_raw(ns, window=window)
    try:
        dec_hits = mem.search(ns, query='', types=['decision'], limit=20)
        dec_msgs = [{'role': 'decision', 'content': h.get('content', '')}
                    for h in dec_hits.get('hits', [])]
        msgs = dec_msgs + msgs
    except Exception:
        pass

    if not msgs:
        return {'updated': False, 'error': 'no logs'}

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            'state': 'running', 'namespace': ns,
            'started_at': time.time(), 'result': None,
        }

    def _run():
        try:
            result = mem_compact.run(ns, msgs, max_chars=max_chars)
            bootstrap = result.get('bootstrap', '')
            upd = mem.compact_update_bootstrap(ns, bootstrap, max_chars=max_chars) \
                  if bootstrap else {'updated': False}
            final = {**upd, 'error': result.get('error'),
                     'backend': 'llm.py', 'model': 'qwen-3-235b'}
            with _jobs_lock:
                _jobs[job_id]['state'] = 'done'
                _jobs[job_id]['result'] = final
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]['state'] = 'error'
                _jobs[job_id]['result'] = {'error': str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return {'job_id': job_id, 'state': 'running', 'namespace': ns,
            'message': 'Compact started (v2: qwen-3-235b, labeled format)'}

# ---------------------------------------------------------------------------
# memory_compact_status
# ---------------------------------------------------------------------------
def tool_memory_compact_status(args: dict) -> dict:
    job_id = (args or {}).get('job_id', '')
    with _jobs_lock:
        if job_id:
            job = _jobs.get(job_id)
            if not job:
                return {'error': f'job {job_id} not found'}
            return {
                'job_id': job_id, 'state': job['state'],
                'namespace': job['namespace'],
                'elapsed_sec': round(time.time() - job['started_at'], 1),
                'result': job.get('result'),
            }
        return {'jobs': {
            k: {'state': v['state'], 'namespace': v['namespace'],
                'elapsed_sec': round(time.time() - v['started_at'], 1)}
            for k, v in list(_jobs.items())[-10:]
        }}

# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------
def tool_memory_search(args: dict) -> dict:
    ns    = (args or {}).get('namespace', '')
    query = (args or {}).get('query', '')
    limit = int((args or {}).get('limit', 10) or 10)
    types = (args or {}).get('types', None)
    return mem.search(ns, query=query, types=types, limit=limit)

# ---------------------------------------------------------------------------
# memory_search_all (cross-namespace)
# ---------------------------------------------------------------------------
def tool_memory_search_all(args: dict) -> dict:
    try:
        from memory_store import search_all
        query = (args or {}).get('query', '')
        limit = int((args or {}).get('limit', 10) or 10)
        types = (args or {}).get('types', None)
        return search_all(query, types=types, limit=limit)
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# memory_append_raw
# ---------------------------------------------------------------------------
def tool_memory_append_raw(args: dict) -> dict:
    ns         = (args or {}).get('namespace', 'mirage-infra')
    content    = (args or {}).get('content', '')
    role       = (args or {}).get('role', 'user')
    importance = int((args or {}).get('importance', 3) or 3)
    tags       = (args or {}).get('tags', [])
    if not content:
        return {'error': 'content required'}
    entry_id = mem.append_entry(
        namespace=ns, type_='raw', content=content,
        role=role, importance=importance, tags=tags,
    )
    return {'success': True, 'id': entry_id}

# ---------------------------------------------------------------------------
# memory_append_decision
# ---------------------------------------------------------------------------
def tool_memory_append_decision(args: dict) -> dict:
    ns         = (args or {}).get('namespace', 'mirage-infra')
    content    = (args or {}).get('content', '')
    title      = (args or {}).get('title', '')
    importance = int((args or {}).get('importance', 3) or 3)
    tags       = (args or {}).get('tags', [])
    if not content:
        return {'error': 'content required'}
    entry_id = mem.append_entry(
        namespace=ns, type_='decision', content=content,
        title=title, importance=importance, tags=tags,
    )
    return {'success': True, 'id': entry_id}

# ---------------------------------------------------------------------------
# memory_decision_auto（LLM抽出）
# ---------------------------------------------------------------------------
def tool_memory_decision_auto(args: dict) -> dict:
    ns        = (args or {}).get('namespace', 'mirage-infra')
    text      = (args or {}).get('text', '')
    max_items = int((args or {}).get('max_items', 8) or 8)

    if not text:
        return {'stored': 0, 'error': 'text required'}

    prompt = (
        'Return JSON only. Key: decisions (array). '
        'Each: {title, decision, rationale, tags, importance}. '
        f'Max {max_items} items.\n\n## Text\n' + text[:2000]
    )
    raw = llm.call(prompt, purpose='compact', max_tokens=800, timeout=30)

    if not raw:
        return {'stored': 0, 'error': 'LLM failed'}

    import json as _j
    try:
        if raw.startswith('```'):
            raw = raw.strip('`').lstrip('json').strip()
        obj = _j.loads(raw)
        if isinstance(obj, list):
            obj = {'decisions': obj}
        items = obj.get('decisions', [])[:max_items]
    except Exception:
        return {'stored': 0, 'error': 'JSON parse failed', 'raw': raw[:200]}

    stored = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        title   = str(item.get('title', 'decision'))
        content = str(item.get('decision', '')).strip()
        if not content:
            continue
        mem.append_entry(
            namespace=ns, type_='decision',
            content=content, title=title,
            importance=int(item.get('importance', 3)),
            tags=item.get('tags', []),
        )
        stored += 1

    return {'stored': stored, 'total': len(items)}

# ---------------------------------------------------------------------------
# memory_supersede
# ---------------------------------------------------------------------------
def tool_memory_supersede(args: dict) -> dict:
    old_id = (args or {}).get('old_id', '')
    new_id = (args or {}).get('new_id', '')
    if not old_id or not new_id:
        return {'error': 'old_id and new_id required'}
    try:
        from memory_store import supersede_entry
        return supersede_entry(old_id, new_id)
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# memory_active_decisions
# ---------------------------------------------------------------------------
def tool_memory_active_decisions(args: dict) -> dict:
    ns    = (args or {}).get('namespace', '')
    limit = int((args or {}).get('limit', 20) or 20)
    try:
        from memory_store import get_active_decisions
        return get_active_decisions(ns, limit=limit)
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# memory_freshness
# ---------------------------------------------------------------------------
def tool_memory_freshness(args: dict) -> dict:
    max_age = int((args or {}).get('max_age_hours', 72) or 72)
    try:
        from memory_store import check_bootstrap_freshness
        return check_bootstrap_freshness(max_age_hours=max_age)
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
TOOLS = {
    'memory_bootstrap': {
        'description': 'Get bootstrap summary for a namespace.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'max_chars': {'type': 'integer'},
        }},
        'handler': tool_memory_bootstrap,
    },
    'memory_compact': {
        'description': 'Compact namespace logs into labeled bootstrap (qwen-3-235b, Ollama-free).',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'max_chars': {'type': 'integer'},
            'window': {'type': 'integer'},
        }},
        'handler': tool_memory_compact,
    },
    'memory_compact_status': {
        'description': 'Check compact job status.',
        'schema': {'type': 'object', 'properties': {
            'job_id': {'type': 'string'},
        }},
        'handler': tool_memory_compact_status,
    },
    'memory_search': {
        'description': 'Search memory entries via FTS.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'query': {'type': 'string'},
            'limit': {'type': 'integer'},
            'types': {'type': 'array', 'items': {'type': 'string'}},
        }, 'required': ['query']},
        'handler': tool_memory_search,
    },
    'memory_search_all': {
        'description': 'Search across ALL namespaces.',
        'schema': {'type': 'object', 'properties': {
            'query': {'type': 'string'},
            'limit': {'type': 'integer'},
        }, 'required': ['query']},
        'handler': tool_memory_search_all,
    },
    'memory_append_raw': {
        'description': 'Append raw entry to memory.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'content': {'type': 'string'},
            'role': {'type': 'string'},
            'importance': {'type': 'integer'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
        }, 'required': ['content']},
        'handler': tool_memory_append_raw,
    },
    'memory_append_decision': {
        'description': 'Append decision entry to memory.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'content': {'type': 'string'},
            'title': {'type': 'string'},
            'importance': {'type': 'integer'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
        }, 'required': ['content']},
        'handler': tool_memory_append_decision,
    },
    'memory_decision_auto': {
        'description': 'Extract decisions using LLM (qwen-3-235b) and store into memory.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'text': {'type': 'string'},
            'max_items': {'type': 'integer'},
        }, 'required': ['text']},
        'handler': tool_memory_decision_auto,
    },
    'memory_supersede': {
        'description': 'Mark old entry as superseded by new one.',
        'schema': {'type': 'object', 'properties': {
            'old_id': {'type': 'string'},
            'new_id': {'type': 'string'},
        }, 'required': ['old_id', 'new_id']},
        'handler': tool_memory_supersede,
    },
    'memory_active_decisions': {
        'description': 'Get only active (non-superseded) decisions for a namespace.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'limit': {'type': 'integer'},
        }},
        'handler': tool_memory_active_decisions,
    },
    'memory_freshness': {
        'description': 'Check bootstrap freshness for all namespaces.',
        'schema': {'type': 'object', 'properties': {
            'max_age_hours': {'type': 'integer'},
        }},
        'handler': tool_memory_freshness,
    },
}

# test
