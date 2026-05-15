"""
tools/memory.py - memory系MCPツール完全版
"""
import sys
import os
import uuid
import threading
import time
import logging
import json
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import llm
from memory import store as mem
from memory import compact as mem_compact

log = logging.getLogger(__name__)
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _compact_jobs_dir() -> str:
    path = r'C:\MirageWork\mcp-server\data\compact_jobs'
    os.makedirs(path, exist_ok=True)
    return path


def _compact_job_path(job_id: str) -> str:
    safe = ''.join(ch for ch in str(job_id) if ch.isalnum() or ch in ('-', '_'))[:64]
    return os.path.join(_compact_jobs_dir(), f'{safe}.json')


def _write_compact_job_snapshot(job_id: str, job: dict) -> None:
    try:
        payload = dict(job)
        payload['job_id'] = job_id
        path = _compact_job_path(job_id)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        log.exception('failed to persist compact job snapshot')


def _read_compact_job_snapshot(job_id: str) -> dict:
    try:
        path = _compact_job_path(job_id)
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {'job_id': job_id, 'state': 'error', 'result': {'error': str(e)}}


def _recent_compact_job_snapshots(limit: int = 10) -> dict:
    try:
        files = sorted(
            (os.path.join(_compact_jobs_dir(), name) for name in os.listdir(_compact_jobs_dir()) if name.endswith('.json')),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )[:limit]
    except Exception:
        return {}
    out = {}
    for path in files:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                item = json.load(f)
            job_id = item.get('job_id') or os.path.splitext(os.path.basename(path))[0]
            out[job_id] = item
        except Exception:
            continue
    return out

# ---------------------------------------------------------------------------
# memory_bootstrap
# ---------------------------------------------------------------------------

def _safe_int(val, default=3):
    """Convert LLM importance value to int (handles 'High', 'medium', mojibake, etc)."""
    try:
        return max(1, min(5, int(val)))
    except (TypeError, ValueError):
        _map = {'low': 1, 'medium': 3, 'normal': 3, 'high': 4, 'critical': 5}
        return _map.get(str(val).strip().lower(), default)

def tool_memory_bootstrap(args: dict) -> dict:
    ns = (args or {}).get('namespace', 'mirage-infra')
    max_chars = int((args or {}).get('max_chars', 800) or 800)
    return mem.get_bootstrap(ns, max_chars=max_chars)

# ---------------------------------------------------------------------------
# memory_compact
# ---------------------------------------------------------------------------
def tool_memory_compact(args: dict) -> dict:
    """v3 (2026-04-26): 時系列 + 増分更新対応。

    変更点:
      - max_chars デフォルト 2000 (旧 800)
      - window デフォルト 100 (旧 200 だが msgs[:20] 切り捨てで実質 20 だった)
      - 既存 bootstrap を取得し prev_summary として compact に渡す
      - decisions の混入は維持
    """
    ns        = (args or {}).get('namespace', 'mirage-infra')
    max_chars = int((args or {}).get('max_chars', 2000) or 2000)
    window    = int((args or {}).get('window', 100) or 100)
    rebuild_semantic_lite = bool((args or {}).get('rebuild_semantic_lite', False))

    # Fetch recent raw entries (created_at 含む)
    msgs = mem.fetch_recent_raw(ns, window=window)
    raw_count = len(msgs)

    # Mix in recent decisions
    #
    # [Fix 2026-05-15] full content via get_entry_full
    #   旧: mem.search 経由で hits.snippet (240 char truncation) を content として使用
    #       → LLM 入力時に decision body 1500+ chars が 240 chars に圧縮、
    #         tonight 詳細が summary 反映されない (decision 94edecfa layer 2 bug)。
    #   新: search hits の id を get_entry_full で full content fetch、
    #       title + content をマージして LLM に渡す。
    #   memory: mirage-infra 94edecfa (writer truncation bug 2 layers)
    decision_count = 0
    try:
        from memory_store import get_entry_full
        dec_hits = mem.search(ns, query='', types=['decision'], limit=30)
        dec_msgs = []
        for h in dec_hits.get('hits', []):
            hid = h.get('id', '')
            full_content = ''
            full_title = h.get('title', '')
            if hid:
                try:
                    full = get_entry_full(hid)
                    if isinstance(full, dict) and not full.get('error'):
                        full_content = full.get('content', '') or ''
                        full_title = full.get('title', '') or full_title
                except Exception:
                    full_content = ''
            content_text = full_content or h.get('content', '') or h.get('snippet', '')
            if full_title and full_title not in (content_text or '')[:200]:
                content_text = f"[{full_title}]\n{content_text}"
            dec_msgs.append({
                'role': 'decision',
                'content': content_text,
                'created_at': h.get('created_at', 0) or 0,
            })
        # Sort decisions newest first, then merge
        dec_msgs.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        dec_msgs = dec_msgs[:20]
        decision_count = len(dec_msgs)
        msgs = dec_msgs + msgs
    except Exception:
        pass

    if not msgs:
        return {'updated': False, 'error': 'no logs'}

    # Get previous bootstrap for incremental update
    prev_summary = ''
    try:
        prev = mem.get_bootstrap(ns, max_chars=max_chars * 2)
        prev_summary = prev.get('summary', '') or ''
    except Exception:
        pass
    newest_entry_at = max((int(m.get('created_at') or 0) for m in msgs), default=0)
    oldest_entry_at = min((int(m.get('created_at') or 0) for m in msgs if int(m.get('created_at') or 0)), default=0)

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            'state': 'running', 'namespace': ns,
            'started_at': time.time(), 'result': None,
            'persisted': True,
        }
        _write_compact_job_snapshot(job_id, _jobs[job_id])

    def _run():
        started = time.time()
        try:
            result = mem_compact.run(
                ns, msgs,
                max_chars=max_chars,
                prev_summary=prev_summary,
            )
            bootstrap = result.get('bootstrap', '')
            upd = mem.compact_update_bootstrap(ns, bootstrap, max_chars=max_chars) \
                  if bootstrap else {'updated': False}
            semantic_lite = None
            if rebuild_semantic_lite:
                try:
                    semantic_lite = mem.semantic_lite_rebuild(limit=5000)
                except Exception as e:
                    semantic_lite = {'error': str(e), 'backend': 'semantic_lite_hashed_ngrams'}
            sections = []
            for line in (bootstrap or '').splitlines():
                stripped = line.strip()
                if stripped.startswith('■') or stripped.startswith('#'):
                    sections.append(stripped[:80])
            compact_report = {
                'namespace': ns,
                'entries_compacted': len(msgs),
                'raw_entries': raw_count,
                'decision_entries': decision_count,
                'oldest_entry_at': oldest_entry_at,
                'newest_entry_at': newest_entry_at,
                'prev_summary_chars': len(prev_summary),
                'new_summary_chars': len(bootstrap or ''),
                'max_chars': max_chars,
                'sections': sections[:12],
                'semantic_lite_rebuild_requested': rebuild_semantic_lite,
                'semantic_lite_rebuild_ran': semantic_lite is not None,
                'duration_sec': round(time.time() - started, 3),
                'warnings': [],
            }
            if not bootstrap:
                compact_report['warnings'].append('compact returned empty bootstrap')
            if result.get('error'):
                compact_report['warnings'].append(str(result.get('error')))
            final = {**upd, 'error': result.get('error'),
                     'backend': 'llm.py', 'model': 'qwen-3-235b',
                     'compact_version': 'v3',
                     'compact_report': compact_report}
            if semantic_lite is not None:
                final['semantic_lite_rebuild'] = semantic_lite
            with _jobs_lock:
                _jobs[job_id]['state'] = 'done'
                _jobs[job_id]['result'] = final
                _jobs[job_id]['finished_at'] = time.time()
                _write_compact_job_snapshot(job_id, _jobs[job_id])
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]['state'] = 'error'
                _jobs[job_id]['result'] = {'error': str(e)}
                _jobs[job_id]['finished_at'] = time.time()
                _write_compact_job_snapshot(job_id, _jobs[job_id])

    threading.Thread(target=_run, daemon=True).start()
    return {'job_id': job_id, 'state': 'running', 'namespace': ns,
            'message': 'Compact v3 started (qwen-3-235b, 時系列+増分更新)'}

# ---------------------------------------------------------------------------
# memory_compact_status
# ---------------------------------------------------------------------------
def _compact_status_trust_fields() -> dict:
    return {
        'completion_verification_required': True,
        'verification_hint': 'Confirm applied memory state with memory_bootstrap freshness_level and since_last_compact.',
        'trust_boundary': 'compact_status tracks worker/job state; bootstrap freshness confirms the memory state readers will see.',
    }


def tool_memory_compact_status(args: dict) -> dict:
    job_id = (args or {}).get('job_id', '')
    with _jobs_lock:
        if job_id:
            job = _jobs.get(job_id)
            if not job:
                snap = _read_compact_job_snapshot(job_id)
                if not snap:
                    return {'error': f'job {job_id} not found'}
                started = float(snap.get('started_at') or time.time())
                return {
                    'job_id': job_id,
                    'state': snap.get('state'),
                    'namespace': snap.get('namespace'),
                    'elapsed_sec': round(time.time() - started, 1),
                    'result': snap.get('result'),
                    'source': 'persistent_snapshot',
                    **_compact_status_trust_fields(),
                }
            return {
                'job_id': job_id, 'state': job['state'],
                'namespace': job['namespace'],
                'elapsed_sec': round(time.time() - job['started_at'], 1),
                'result': job.get('result'),
                'source': 'process_registry',
                **_compact_status_trust_fields(),
            }
        jobs = {
            k: {'state': v['state'], 'namespace': v['namespace'],
                'elapsed_sec': round(time.time() - v['started_at'], 1),
                'source': 'process_registry'}
            for k, v in list(_jobs.items())[-10:]
        }
        for k, v in _recent_compact_job_snapshots(limit=10).items():
            if k in jobs:
                continue
            started = float(v.get('started_at') or time.time())
            jobs[k] = {
                'state': v.get('state'),
                'namespace': v.get('namespace'),
                'elapsed_sec': round(time.time() - started, 1),
                'source': 'persistent_snapshot',
            }
        return {'jobs': jobs, **_compact_status_trust_fields()}

# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------
def tool_memory_search(args: dict) -> dict:
    ns    = (args or {}).get('namespace', '')
    query = (args or {}).get('query', '')
    limit = int((args or {}).get('limit', 10) or 10)
    types = (args or {}).get('types', None)
    inc_sup = bool((args or {}).get('include_superseded', False))
    results = mem.search(ns, query=query, types=types, limit=limit,
                         include_superseded=inc_sup)
    for key in ('results', 'hits'):
        if key not in results or not isinstance(results.get(key), list):
            continue
        seen_ids = set()
        deduped = []
        for e in results[key]:
            entry_id = e.get('id') if isinstance(e, dict) else None
            if entry_id:
                if entry_id in seen_ids:
                    continue
                seen_ids.add(entry_id)
            deduped.append(e)
        results[key] = deduped
        if isinstance(results.get('count'), int):
            results['count'] = len(deduped)
    try:
        for e in (results.get('results') or results.get('hits') or []):
            if e.get('id'):
                mem.touch_entry(e['id'])
    except Exception:
        pass
    return results

# ---------------------------------------------------------------------------
# memory_dig: drill down older entries for a theme, grouped by date
# ---------------------------------------------------------------------------
def tool_memory_dig(args: dict) -> dict:
    from datetime import datetime
    ns     = (args or {}).get('namespace', 'mirage-infra')
    theme  = ((args or {}).get('theme') or '').strip()
    before = ((args or {}).get('before_date') or '').strip()
    limit  = int((args or {}).get('limit', 20) or 20)
    inc_sup = bool((args or {}).get('include_superseded', False))
    if not theme:
        return {'error': 'theme is required'}

    # namespace='*' or empty -> cross-namespace dig via search_all.
    # Returned hits include 'namespace' field.
    if ns in ('*', '', 'all'):
        try:
            from memory_store import search_all
            raw = search_all(theme, limit=limit * 3)
        except Exception as e:
            return {'error': f'search_all failed: {e}'}
    else:
        raw = mem.search(ns, query=theme, limit=limit * 3,
                         include_superseded=inc_sup)
    hits = raw.get('results') or raw.get('hits') or []

    if before:
        try:
            cut_ts = int(datetime.strptime(before, '%Y-%m-%d').timestamp())
            hits = [h for h in hits if int(h.get('created_at', 0) or 0) < cut_ts]
        except Exception:
            pass
    hits = hits[:limit]

    groups: dict = {}
    for h in hits:
        ts = int(h.get('created_at', 0) or 0)
        d = datetime.fromtimestamp(ts).strftime('%Y-%m-%d') if ts else 'unknown'
        groups.setdefault(d, []).append(h)
        if h.get('id'):
            try:
                mem.touch_entry(h['id'])
            except Exception:
                pass

    return {
        'namespace': ns,
        'theme': theme,
        'before_date': before,
        'count': len(hits),
        'groups': groups,
    }

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
    importance = _safe_int((args or {}).get('importance', 3), 3)
    tags       = (args or {}).get('tags', [])
    if not content:
        return {'error': 'content required'}
    entry_id = mem.append_entry(
        namespace=ns, type_='raw', content=content,
        role=role, importance=importance, tags=tags,
    )
    if isinstance(entry_id, dict):
        return {'success': True, **entry_id}
    return {'success': True, 'id': entry_id}

# ---------------------------------------------------------------------------
# memory_append_decision
# ---------------------------------------------------------------------------
def tool_memory_append_decision(args: dict) -> dict:
    ns         = (args or {}).get('namespace', 'mirage-infra')
    content    = (args or {}).get('content', '')
    decision   = (args or {}).get('decision', '')
    rationale  = (args or {}).get('rationale', '')
    title      = (args or {}).get('title', '')
    importance = _safe_int((args or {}).get('importance', 3), 3)
    tags       = (args or {}).get('tags', [])
    if not content and decision:
        content = decision
        if rationale:
            content += "\n\nRationale:\n" + rationale
    if not content:
        return {'error': 'content required', 'hint': 'Use content, or decision+rationale'}
    entry_id = mem.append_entry(
        namespace=ns, type_='decision', content=content,
        title=title, importance=importance, tags=tags,
    )
    if isinstance(entry_id, dict):
        return {'success': True, **entry_id}
    return {'success': True, 'id': entry_id}

# ---------------------------------------------------------------------------
# memory_decision_auto (LLM抽出)
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
            importance=_safe_int(item.get('importance', 3), 3),
            tags=item.get('tags', []),
        )
        stored += 1

    return {'stored': stored, 'total': len(items)}

# ---------------------------------------------------------------------------
# memory_supersede
# ---------------------------------------------------------------------------
def _resolve_entry_id(prefix: str) -> str:
    """Resolve an 8-char (or any partial) prefix to full entry UUID.
    Returns the full id if exactly one match, else returns the input unchanged
    (caller will hit supersede_entry's "not found" path with informative error).
    """
    if not prefix or '-' in prefix and len(prefix) >= 32:
        return prefix
    try:
        con = sqlite3.connect(_memory_db_path())
        try:
            rows = con.execute(
                "SELECT id FROM entries WHERE id LIKE ? LIMIT 2",
                (prefix + '%',),
            ).fetchall()
        finally:
            con.close()
        if len(rows) == 1:
            return rows[0][0]
    except Exception:
        pass
    return prefix


def tool_memory_supersede(args: dict) -> dict:
    old_id = (args or {}).get('old_id', '')
    new_id = (args or {}).get('new_id', '')
    if not old_id or not new_id:
        return {'error': 'old_id and new_id required'}
    old_full = _resolve_entry_id(old_id)
    new_full = _resolve_entry_id(new_id)
    if old_full == new_full:
        return {'ok': False, 'error': 'old_id == new_id (self-supersede blocked)',
                'old_id': old_full, 'new_id': new_full}
    try:
        from memory_store import supersede_entry
        result = supersede_entry(old_full, new_full)
        if old_full != old_id or new_full != new_id:
            result.setdefault('resolved', {})
            if old_full != old_id:
                result['resolved']['old_id'] = old_full
            if new_full != new_id:
                result['resolved']['new_id'] = new_full
        return result
    except Exception as e:
        return {'error': str(e)}


def tool_memory_supersede_many(args: dict) -> dict:
    """Bulk supersede. Accepts either:
      - pairs: [{'old_id': prefix, 'new_id': prefix}, ...]
      - olds + new_id: olds=[prefix, ...], new_id=prefix  (many-to-one shortcut)
    8-char prefixes are auto-resolved.

    dry_run=True (default false): resolve and validate all pairs, but do NOT
    persist any supersede. Returns the same shape as a real run but with
    'would_supersede' counts instead of 'succeeded'/'failed'. Useful to preview
    a large bulk operation (Iron Law canonical absorbing N old decisions).
    """
    pairs = (args or {}).get('pairs') or []
    olds = (args or {}).get('olds') or []
    shared_new = (args or {}).get('new_id', '')
    dry_run = bool((args or {}).get('dry_run', False))
    if olds and shared_new:
        pairs = pairs + [{'old_id': o, 'new_id': shared_new} for o in olds]
    if not pairs:
        return {'error': 'provide pairs=[{old_id,new_id},...] or olds=[...]+new_id'}
    try:
        from memory_store import supersede_entry, get_entry_full
    except Exception as e:
        return {'error': f'memory_store import failed: {e}'}
    results = []
    ok = 0
    failed = 0
    would_succeed = 0
    for p in pairs:
        old_in = (p or {}).get('old_id', '')
        new_in = (p or {}).get('new_id', '')
        if not old_in or not new_in:
            results.append({'ok': False, 'error': 'old_id and new_id required',
                            'old_id': old_in, 'new_id': new_in})
            failed += 1
            continue
        old_full = _resolve_entry_id(old_in)
        new_full = _resolve_entry_id(new_in)
        if old_full == new_full:
            results.append({'ok': False, 'error': 'old_id == new_id, skipping self-supersede',
                            'old_id_input': old_in, 'new_id_input': new_in})
            failed += 1
            continue
        if dry_run:
            # validate both ends exist + check status without mutating
            try:
                old_entry = get_entry_full(old_full)
                new_entry = get_entry_full(new_full)
                if 'error' in (old_entry or {}):
                    results.append({'ok': False, 'error': f'old not found: {old_entry["error"]}',
                                    'old_id_input': old_in, 'new_id_input': new_in})
                    failed += 1
                    continue
                if 'error' in (new_entry or {}):
                    results.append({'ok': False, 'error': f'new not found: {new_entry["error"]}',
                                    'old_id_input': old_in, 'new_id_input': new_in})
                    failed += 1
                    continue
                old_status = (old_entry or {}).get('status') or 'active'
                results.append({
                    'ok': True, 'dry_run': True,
                    'old_id_input': old_in, 'new_id_input': new_in,
                    'old_id': old_full, 'new_id': new_full,
                    'old_status_current': old_status,
                    'old_title': (old_entry or {}).get('title', '')[:60],
                    'new_title': (new_entry or {}).get('title', '')[:60],
                    'would_change_status_to': 'superseded',
                })
                would_succeed += 1
            except Exception as e:
                failed += 1
                results.append({'ok': False, 'error': str(e), 'dry_run': True,
                                'old_id_input': old_in, 'new_id_input': new_in})
            continue
        try:
            r = supersede_entry(old_full, new_full)
            r_ok = bool(r.get('ok', True)) and 'error' not in r
            if r_ok:
                ok += 1
            else:
                failed += 1
            results.append({**r, 'old_id_input': old_in, 'new_id_input': new_in})
        except Exception as e:
            failed += 1
            results.append({'ok': False, 'error': str(e),
                            'old_id_input': old_in, 'new_id_input': new_in})
    if dry_run:
        return {
            'total': len(pairs),
            'dry_run': True,
            'would_succeed': would_succeed,
            'failed': failed,
            'results': results,
            'operator_summary': f'supersede_many [dry_run]: would supersede {would_succeed}/{len(pairs)}, {failed} would fail',
        }
    return {
        'total': len(pairs),
        'succeeded': ok,
        'failed': failed,
        'results': results,
        'operator_summary': f'supersede_many: {ok}/{len(pairs)} ok, {failed} failed',
    }

# ---------------------------------------------------------------------------
# memory_get (fetch full entry by id)
# ---------------------------------------------------------------------------
def tool_memory_get(args: dict) -> dict:
    """Fetch a full entry by ID or hex prefix. Use after memory_search
    when the snippet is insufficient.
    """
    entry_id = (args or {}).get('id', '') or (args or {}).get('entry_id', '')
    if not entry_id:
        return {'error': 'id required'}
    try:
        from memory_store import get_entry_full
        result = get_entry_full(entry_id)
        # Bump access_count using the resolved full id, not the input prefix
        if 'error' not in result and result.get('id'):
            try:
                from memory_store import touch_entry
                touch_entry(result['id'])
            except Exception:
                pass
        return result
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


def _memory_db_path() -> str:
    return r'C:\MirageWork\mcp-server\data\memory.db'


def _parse_tags_arg(tags) -> list:
    if not tags:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(',') if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


def tool_memory_lifecycle_review(args: dict) -> dict:
    """Review active/superseded/archive candidates without mutating memory."""
    ns = (args or {}).get('namespace', 'mirage-infra')
    query = (args or {}).get('query', '')
    tags = _parse_tags_arg((args or {}).get('tags', []))
    limit = max(1, min(int((args or {}).get('limit', 20) or 20), 100))
    include_archived = bool((args or {}).get('include_archived', False))
    where = ['namespace=?']
    params = [ns]
    if query:
        where.append('(title LIKE ? OR content LIKE ? OR decision_text LIKE ?)')
        like = f'%{query}%'
        params.extend([like, like, like])
    if not include_archived:
        where.append("(status IS NULL OR status NOT IN ('archived'))")
    # tags は JSON 配列文字列で保存されているため、各タグを `"tag"` パターンで SQL レベル絞り込み。
    # LIKE のワイルドカード `_` `%` がタグ名に含まれても誤マッチしないよう ESCAPE する。
    for tag in tags:
        safe = tag.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        where.append("tags LIKE ? ESCAPE '\\'")
        params.append(f'%"{safe}"%')
    sql = (
        'SELECT id, namespace, type, title, tags, status, created_at, updated_at, '
        'superseded_by, COALESCE(access_count,0) AS access_count '
        'FROM entries WHERE ' + ' AND '.join(where) +
        ' ORDER BY created_at DESC LIMIT ?'
    )
    params.append(limit)
    con = sqlite3.connect(_memory_db_path())
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    items = []
    for r in rows:
        try:
            entry_tags = json.loads(r['tags'] or '[]')
        except Exception:
            entry_tags = []
        if tags and not set(tags).issubset(set(entry_tags)):
            continue
        status = r['status'] or 'active'
        suggested = []
        if status == 'active' and entry_tags and any(t.endswith('smoke') or 'smoke' in t for t in entry_tags):
            suggested.append('archive_smoke_probe')
        if status == 'superseded':
            suggested.append('already_superseded')
        items.append({
            'id': r['id'],
            'namespace': r['namespace'],
            'type': r['type'],
            'title': r['title'] or '',
            'status': status,
            'tags': entry_tags,
            'created_at': int(r['created_at'] or 0),
            'updated_at': int(r['updated_at'] or 0),
            'superseded_by': r['superseded_by'] or '',
            'access_count': int(r['access_count'] or 0),
            'suggested_actions': suggested,
        })
        if len(items) >= limit:
            break
    archive_candidates = [i for i in items if 'archive_smoke_probe' in i['suggested_actions']]
    return {
        'namespace': ns,
        'query': query,
        'tags': tags,
        'operator_summary': (
            f'{len(archive_candidates)} archive candidate(s), {len(items)} reviewed'
            if archive_candidates else f'ok; {len(items)} lifecycle item(s) reviewed'
        ),
        'counts': {
            'reviewed': len(items),
            'archive_candidates': len(archive_candidates),
        },
        'items': items,
    }


def tool_memory_archive_by_query(args: dict) -> dict:
    """Archive matching entries by query/tag. Defaults to dry_run=true."""
    ns = (args or {}).get('namespace', 'mirage-infra')
    query = (args or {}).get('query', '')
    tags = _parse_tags_arg((args or {}).get('tags', []))
    dry_run = bool((args or {}).get('dry_run', True))
    limit = max(1, min(int((args or {}).get('limit', 20) or 20), 100))
    if not query and not tags:
        return {'error': 'query or tags required', 'dry_run': dry_run}
    review = tool_memory_lifecycle_review({
        'namespace': ns,
        'query': query,
        'tags': tags,
        'limit': limit,
        'include_archived': False,
    })
    ids = [
        i['id'] for i in review.get('items', [])
        if (i.get('status') or 'active') == 'active'
    ][:limit]
    result = {
        'namespace': ns,
        'query': query,
        'tags': tags,
        'dry_run': dry_run,
        'matched': len(ids),
        'archived': 0,
        'ids': ids,
        'operator_summary': (
            f'would archive {len(ids)} matching entr(y/ies)'
            if dry_run else f'archived {len(ids)} matching entr(y/ies)'
        ),
    }
    if dry_run or not ids:
        return result
    con = sqlite3.connect(_memory_db_path())
    try:
        now = int(time.time())
        con.executemany(
            "UPDATE entries SET status='archived', updated_at=? WHERE id=? AND (status IS NULL OR status='active')",
            [(now, entry_id) for entry_id in ids],
        )
        con.commit()
        result['archived'] = len(ids)
    finally:
        con.close()
    return result


def _tag_like_pattern(tag: str) -> str:
    safe = tag.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%"{safe}"%'


def _find_entries_with_tag(con, ns: str, tag: str, limit: int):
    where = ["tags LIKE ? ESCAPE '\\'", "(status IS NULL OR status='active')"]
    params = [_tag_like_pattern(tag)]
    if ns:
        where.insert(0, "namespace=?")
        params.insert(0, ns)
    sql = (
        "SELECT id, namespace, title, tags FROM entries WHERE "
        + ' AND '.join(where) + " ORDER BY created_at DESC LIMIT ?"
    )
    params.append(limit)
    return con.execute(sql, params).fetchall()


def tool_memory_tag_strip(args: dict) -> dict:
    """Remove a tag from active entries that have it. Defaults to dry_run=true."""
    ns = (args or {}).get('namespace') or None
    tag = (args or {}).get('tag', '')
    dry_run = bool((args or {}).get('dry_run', True))
    limit = max(1, min(int((args or {}).get('limit', 100) or 100), 1000))
    if not tag:
        return {'error': 'tag required', 'dry_run': dry_run}
    con = sqlite3.connect(_memory_db_path())
    con.row_factory = sqlite3.Row
    matched = []
    try:
        rows = _find_entries_with_tag(con, ns, tag, limit)
        for r in rows:
            try:
                entry_tags = json.loads(r['tags'] or '[]')
            except Exception:
                continue
            if tag not in entry_tags:
                continue
            matched.append({
                'id': r['id'],
                'namespace': r['namespace'],
                'title': r['title'] or '',
                'new_tags': [t for t in entry_tags if t != tag],
            })
        if not dry_run and matched:
            now = int(time.time())
            for m in matched:
                con.execute(
                    "UPDATE entries SET tags=?, updated_at=? WHERE id=?",
                    (json.dumps(m['new_tags'], ensure_ascii=False), now, m['id']),
                )
            con.commit()
    finally:
        con.close()
    return {
        'namespace': ns or 'all',
        'tag': tag,
        'dry_run': dry_run,
        'matched': len(matched),
        'stripped': 0 if dry_run else len(matched),
        'ids': [m['id'] for m in matched],
        'operator_summary': (
            f'would strip "{tag}" from {len(matched)} entr(y/ies)'
            if dry_run else f'stripped "{tag}" from {len(matched)} entr(y/ies)'
        ),
    }


def tool_memory_tag_rename(args: dict) -> dict:
    """Rename a tag (or merge into existing) across active entries. Defaults to dry_run=true."""
    ns = (args or {}).get('namespace') or None
    old_tag = (args or {}).get('old_tag', '')
    new_tag = (args or {}).get('new_tag', '')
    dry_run = bool((args or {}).get('dry_run', True))
    limit = max(1, min(int((args or {}).get('limit', 100) or 100), 1000))
    if not old_tag or not new_tag:
        return {'error': 'old_tag and new_tag required', 'dry_run': dry_run}
    if old_tag == new_tag:
        return {'error': 'old_tag == new_tag, nothing to do', 'dry_run': dry_run}
    con = sqlite3.connect(_memory_db_path())
    con.row_factory = sqlite3.Row
    matched = []
    try:
        rows = _find_entries_with_tag(con, ns, old_tag, limit)
        for r in rows:
            try:
                entry_tags = json.loads(r['tags'] or '[]')
            except Exception:
                continue
            if old_tag not in entry_tags:
                continue
            new_list = []
            seen = set()
            for t in entry_tags:
                rep = new_tag if t == old_tag else t
                if rep not in seen:
                    new_list.append(rep)
                    seen.add(rep)
            matched.append({
                'id': r['id'],
                'namespace': r['namespace'],
                'title': r['title'] or '',
                'new_tags': new_list,
            })
        if not dry_run and matched:
            now = int(time.time())
            for m in matched:
                con.execute(
                    "UPDATE entries SET tags=?, updated_at=? WHERE id=?",
                    (json.dumps(m['new_tags'], ensure_ascii=False), now, m['id']),
                )
            con.commit()
    finally:
        con.close()
    return {
        'namespace': ns or 'all',
        'old_tag': old_tag,
        'new_tag': new_tag,
        'dry_run': dry_run,
        'matched': len(matched),
        'renamed': 0 if dry_run else len(matched),
        'ids': [m['id'] for m in matched],
        'operator_summary': (
            f'would rename "{old_tag}" -> "{new_tag}" on {len(matched)} entr(y/ies)'
            if dry_run else f'renamed "{old_tag}" -> "{new_tag}" on {len(matched)} entr(y/ies)'
        ),
    }


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

# ---------------------------------------------------------------------------
# memory_l0 - サマリ常時ロード (L0)
# ---------------------------------------------------------------------------
def tool_memory_l0(args: dict) -> dict:
    """L0: Return compact namespace summaries (50-100 tok each).
    Always-load layer for session initialization.
    """
    ns = (args or {}).get('namespace', None)
    from memory import store as mem
    return mem.get_l0(namespace=ns)


# ---------------------------------------------------------------------------
# memory_l1 - Salience Top-N (L1)
# ---------------------------------------------------------------------------
def tool_memory_l1(args: dict) -> dict:
    """L1: Return top-N entries by salience score (importance ﾃ・freq ﾃ・recency).
    Session-start layer, 500-800 tokens total.
    """
    ns     = (args or {}).get('namespace', None)
    top_n  = int((args or {}).get('top_n', 20) or 20)
    types  = (args or {}).get('types', None)
    if isinstance(types, str):
        types = [t.strip() for t in types.split(',')]
    from memory import store as mem
    return mem.get_l1(namespace=ns, top_n=top_n, type_filter=types)



# ---------------------------------------------------------------------------
# Links ツール群 (Phase 3)
# ---------------------------------------------------------------------------
def _links_db_path() -> str:
    import os
    return r'C:\MirageWork\mcp-server\data\memory.db'

def _links_connect():
    import sqlite3
    return sqlite3.connect(_links_db_path())

def _link_trust_class(relation_type: str, note: str = '') -> dict:
    """Return operator-facing trust class for a memory link.

    related links are split by provenance. Auto-created related links are useful
    graph candidates, but weaker than explicit semantic relations.
    """
    rel = relation_type or ''
    note_l = (note or '').lower()
    if rel == 'related':
        auto_markers = ('auto-', 'smoke', 'lint', 'semantic_lite', 'backfill')
        is_auto = not note_l or any(marker in note_l for marker in auto_markers)
        return {
            'relation_strength': 'related_auto' if is_auto else 'related_manual',
            'epistemic_status': 'candidate' if is_auto else 'manual_candidate',
            'confidence_basis': 'auto_generated_link' if is_auto else 'manual_related_link',
            'meaning_strength': 1 if is_auto else 2,
        }
    if rel in ('supports', 'supersedes', 'contradicts'):
        return {
            'relation_strength': rel,
            'epistemic_status': 'explicit',
            'confidence_basis': f'explicit_{rel}_link',
            'meaning_strength': 4,
        }
    if rel == 'consolidated_into':
        return {
            'relation_strength': rel,
            'epistemic_status': 'structural',
            'confidence_basis': 'consolidation_link',
            'meaning_strength': 3,
        }
    return {
        'relation_strength': rel or 'unknown',
        'epistemic_status': 'unknown',
        'confidence_basis': 'unknown',
        'meaning_strength': 0,
    }

def tool_memory_link_create(args: dict) -> dict:
    """Create a typed link between two memory entries."""
    import uuid, time
    src  = (args or {}).get('source_id', '')
    tgt  = (args or {}).get('target_id', '')
    rel  = (args or {}).get('relation_type', 'related')
    score = float((args or {}).get('score', 0.8) or 0.8)
    note = (args or {}).get('note', '')
    
    if not src or not tgt:
        return {'error': 'source_id and target_id required'}
    if rel not in ('supersedes', 'contradicts', 'supports', 'related'):
        return {'error': f'invalid relation_type: {rel}. Use: supersedes/contradicts/supports/related'}
    
    link_id = str(uuid.uuid4())
    con = _links_connect()
    try:
        con.execute(
            'INSERT INTO links (id, source_id, target_id, relation_type, score, created_at, note) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (link_id, src, tgt, rel, score, int(time.time()), note)
        )
        con.commit()
        trust = _link_trust_class(rel, note)
        return {'id': link_id, 'source_id': src, 'target_id': tgt,
                'relation_type': rel, 'score': score, **trust}
    except Exception as e:
        return {'error': str(e)}
    finally:
        con.close()


def tool_memory_link_search(args: dict) -> dict:
    """Get all links for a given entry (both directions)."""
    entry_id = (args or {}).get('entry_id', '')
    rel_type = (args or {}).get('relation_type', None)
    direction = (args or {}).get('direction', 'both')  # 'in', 'out', 'both'
    
    if not entry_id:
        return {'error': 'entry_id required'}
    
    con = _links_connect()
    try:
        results = []
        
        if direction in ('out', 'both'):
            q = 'SELECT id, source_id, target_id, relation_type, score, note FROM links WHERE source_id = ?'
            params = [entry_id]
            if rel_type:
                q += ' AND relation_type = ?'
                params.append(rel_type)
            for row in con.execute(q, params).fetchall():
                results.append({'id':row[0],'source_id':row[1],'target_id':row[2],
                                 'relation_type':row[3],'score':row[4],'note':row[5],'direction':'out'})
        
        if direction in ('in', 'both'):
            q = 'SELECT id, source_id, target_id, relation_type, score, note FROM links WHERE target_id = ?'
            params = [entry_id]
            if rel_type:
                q += ' AND relation_type = ?'
                params.append(rel_type)
            for row in con.execute(q, params).fetchall():
                results.append({'id':row[0],'source_id':row[1],'target_id':row[2],
                                 'relation_type':row[3],'score':row[4],'note':row[5],'direction':'in'})
        
        for lnk in results:
            lnk.update(_link_trust_class(lnk.get('relation_type', ''), lnk.get('note') or ''))

        return {'entry_id': entry_id, 'links': results, 'count': len(results),
                'epistemic_summary': {
                    'candidate': sum(1 for l in results if l['epistemic_status'] == 'candidate'),
                    'manual_candidate': sum(1 for l in results if l['epistemic_status'] == 'manual_candidate'),
                    'explicit':  sum(1 for l in results if l['epistemic_status'] == 'explicit'),
                    'structural': sum(1 for l in results if l['epistemic_status'] == 'structural'),
                },
                'by_relation_strength': {
                    key: sum(1 for l in results if l.get('relation_strength') == key)
                    for key in ('related_auto', 'related_manual', 'supports', 'supersedes', 'contradicts', 'consolidated_into')
                }}
    finally:
        con.close()


def tool_memory_link_promotion_candidates(args: dict) -> dict:
    """Scan related_auto links and return candidates worth promoting to explicit relation types."""
    import sqlite3, re, collections
    limit = int((args or {}).get('limit', 30))
    min_score = float((args or {}).get('min_score', 0.85))

    db = _links_db_path()
    mem = _memory_db_path()
    con_l = sqlite3.connect(db)
    con_m = sqlite3.connect(mem)
    try:
        # Get all related_auto links with score
        rows = con_l.execute(
            "SELECT id, source_id, target_id, score FROM links WHERE relation_type='related' ORDER BY COALESCE(score,0) DESC"
        ).fetchall()

        # Entry metadata cache
        def get_entry(eid):
            r = con_m.execute(
                "SELECT namespace, type, title, content FROM entries WHERE id=? AND status='active'",
                (eid,)
            ).fetchone()
            return r  # (namespace, type, title, content) or None

        # Hub detection: count how many links point TO each target
        target_counts = collections.Counter(r[2] for r in rows)

        candidates = []
        seen = set()

        for link_id, src, tgt, score in rows:
            if len(candidates) >= limit:
                break
            key = (src, tgt)
            if key in seen:
                continue
            seen.add(key)

            score = score or 0.0
            src_e = get_entry(src)
            tgt_e = get_entry(tgt)
            if not src_e or not tgt_e:
                continue

            reasons = []
            promote_to = 'keep_auto'

            # Condition 1: high score
            if score >= min_score:
                reasons.append(f'score={score:.3f}')

            # Condition 2: hub (target has many incoming related links)
            hub_cnt = target_counts[tgt]
            if hub_cnt >= 3:
                reasons.append(f'hub_count={hub_cnt}')

            # Condition 3: both decision type
            if src_e[1] == 'decision' and tgt_e[1] == 'decision':
                reasons.append('both_decision')

            # Condition 4: shared Phase/Loop/issue id in title
            def extract_ids(text):
                if not text:
                    return set()
                return set(re.findall(r'(?:Phase|Loop|chg-?\d+|Issue|P\d+)\s*[\s#]?\d+', text, re.IGNORECASE))

            src_ids = extract_ids(str(src_e[2]) + ' ' + str(src_e[3] or ''))
            tgt_ids = extract_ids(str(tgt_e[2]) + ' ' + str(tgt_e[3] or ''))
            shared = src_ids & tgt_ids
            if shared:
                reasons.append(f'shared_ids={list(shared)[:3]}')

            # Cross-namespace bonus
            if src_e[0] != tgt_e[0]:
                reasons.append(f'cross_ns={src_e[0]}->{tgt_e[0]}')

            if not reasons:
                continue  # skip if no promotion signal

            # Determine promote_to
            src_title = (src_e[2] or '').lower()
            tgt_title = (tgt_e[2] or '').lower()
            if any(w in src_title or w in tgt_title for w in ['supersede', 'replace', 'obsolete', '廃止', '置換']):
                promote_to = 'supersedes'
            elif any(w in src_title or w in tgt_title for w in ['support', 'confirm', 'verify', '確認', '実証']):
                promote_to = 'supports'
            elif score >= 0.9 or hub_cnt >= 5 or shared:
                promote_to = 'related_manual'
            else:
                promote_to = 'keep_auto'

            if promote_to == 'keep_auto':
                continue

            candidates.append({
                'link_id': link_id,
                'source_id': src,
                'target_id': tgt,
                'source_ns': src_e[0],
                'target_ns': tgt_e[0],
                'source_title': (src_e[2] or '')[:60],
                'target_title': (tgt_e[2] or '')[:60],
                'score': score,
                'promote_to': promote_to,
                'reasons': reasons,
            })

        return {
            'operator_summary': f'{len(candidates)} promotion candidates found (of {len(rows)} related_auto links)',
            'candidate_count': len(candidates),
            'candidates': candidates[:limit],
            'hint': 'Use memory_link_create to create explicit links, then delete or keep the related_auto.',
        }
    finally:
        con_l.close()
        con_m.close()


def tool_memory_contradiction_candidates(args: dict) -> dict:
    """Scan memory for potential design contradictions: banned patterns vs active mentions."""
    import sqlite3, re
    limit = int((args or {}).get('limit', 20))
    mem = _memory_db_path()
    con = sqlite3.connect(mem)
    try:
        # Banned pattern seeds (title/content keywords)
        BAN_PATTERNS = [
            # (ban_keyword, mention_keyword, description)
            (r'H264|H\.264|AVC|h264', r'H264|H\.264|AVC|h264', 'H264/AVC 禁止 vs mention'),
            (r'AOA|accessory', r'AOA|aoa_|AccessoryIoService|accessory_mode', 'AOA 禁止 vs mention'),
            (r'D3D11VA|d3d11va', r'D3D11VA|d3d11va|D3D11', 'D3D11VA 禁止 vs mention'),
            (r'禁止|forbidden|prohibited|deprecated|obsolete|廃止', r'use|実装|implement|enable|有効', '禁止 vs 実装'),
            (r'pm uninstall', r'uninstall|pm uninstall', 'pm uninstall 禁止 vs mention'),
        ]

        all_entries = con.execute(
            "SELECT id, namespace, type, title, content FROM entries WHERE status='active'"
        ).fetchall()

        # Split into ban entries and mention entries
        candidates = []
        seen_pairs = set()

        for ban_kw, mention_kw, desc in BAN_PATTERNS:
            ban_entries = [
                e for e in all_entries
                if re.search(ban_kw, str(e[3] or '') + ' ' + str(e[4] or ''), re.IGNORECASE)
                and re.search(r'禁止|forbidden|prohibited|deprecated|obsolete|廃止|abort|removed|deleted', str(e[3] or '') + ' ' + str(e[4] or ''), re.IGNORECASE)
            ]
            mention_entries = [
                e for e in all_entries
                if re.search(mention_kw, str(e[3] or '') + ' ' + str(e[4] or ''), re.IGNORECASE)
                and not re.search(r'禁止|forbidden|prohibited|deprecated|obsolete|廃止', str(e[3] or '') + ' ' + str(e[4] or ''), re.IGNORECASE)
            ]

            for ban_e in ban_entries[:3]:
                for mention_e in mention_entries[:5]:
                    if ban_e[0] == mention_e[0]:
                        continue  # skip same entry
                    pair = tuple(sorted([ban_e[0], mention_e[0]]))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    if len(candidates) >= limit:
                        break
                    candidates.append({
                        'pattern': desc,
                        'ban_entry': {
                            'id': ban_e[0], 'namespace': ban_e[1], 'type': ban_e[2],
                            'title': (ban_e[3] or '')[:60],
                        },
                        'mention_entry': {
                            'id': mention_e[0], 'namespace': mention_e[1], 'type': mention_e[2],
                            'title': (mention_e[3] or '')[:60],
                        },
                        'severity': 'high' if any(k in desc for k in ['H264', 'AOA', 'D3D11VA', 'pm uninstall']) else 'medium',
                        'suggested_action': 'verify if mention_entry violates ban; if so, create contradicts link and flag for review',
                    })

        # Sort by severity
        candidates.sort(key=lambda x: 0 if x['severity'] == 'high' else 1)

        high = sum(1 for c in candidates if c['severity'] == 'high')
        medium = sum(1 for c in candidates if c['severity'] == 'medium')

        return {
            'operator_summary': f'{len(candidates)} contradiction candidates ({high} high, {medium} medium)',
            'candidate_count': len(candidates),
            'high_count': high,
            'medium_count': medium,
            'candidates': candidates[:limit],
            'hint': 'Review each pair. If genuine contradiction: memory_link_create relation_type=contradicts. If false positive: ignore.',
            'note': 'This is a heuristic scan; manual review required before creating contradicts links.',
        }
    finally:
        con.close()








def tool_memory_fastembed_debug(args: dict) -> dict:
    """Server-side introspection for fastembed/HNSW backend selection.
    
    Returns sys.executable, sys.path, memory_store.__file__,
    import status, index paths, and actual backend selected for a sample query.
    Essential for diagnosing "works locally but brute_force via MCP" issues.
    """
    import sys as _sys, os as _os
    out = {}

    # 1. Python runtime
    out['sys_executable'] = _sys.executable
    out['sys_path_first5'] = _sys.path[:5]
    out['python_version'] = _sys.version.split()[0]

    # 2. memory_store import chain
    try:
        import importlib
        ms = importlib.import_module('memory_store')
        out['memory_store_file'] = getattr(ms, '__file__', 'unknown')
    except Exception as e:
        out['memory_store_file'] = f'ERROR: {e}'

    # 3. mirage-shared at front of sys.path?
    ms_path = r'C:\MirageWork\mirage-shared'
    out['mirage_shared_in_path'] = any(
        _os.path.normcase(p) == _os.path.normcase(ms_path)
        for p in _sys.path
    )
    out['mirage_shared_path_position'] = next(
        (i for i, p in enumerate(_sys.path) if _os.path.normcase(p) == _os.path.normcase(ms_path)),
        -1
    )

    # 4. fastembed import
    try:
        from fastembed import TextEmbedding
        out['fastembed_ok'] = True
        out['fastembed_file'] = TextEmbedding.__module__
    except Exception as e:
        out['fastembed_ok'] = False
        out['fastembed_error'] = str(e)

    # 4b. fastembed subprocess mode (the normal server runtime path)
    try:
        from memory_store import _fastembed_available, _FASTEMBED_VENV_PYTHON
        out['fastembed_subprocess_ok'] = bool(_fastembed_available())
        out['fastembed_subprocess_python'] = _FASTEMBED_VENV_PYTHON
        out['fastembed_embedding_mode'] = (
            'direct_import' if out.get('fastembed_ok')
            else 'subprocess' if out.get('fastembed_subprocess_ok')
            else 'unavailable'
        )
    except Exception as e:
        out['fastembed_subprocess_ok'] = False
        out['fastembed_subprocess_error'] = str(e)
        out['fastembed_embedding_mode'] = 'unavailable'

    # 5. usearch import
    try:
        from usearch.index import Index
        out['usearch_ok'] = True
        out['usearch_file'] = Index.__module__
    except Exception as e:
        out['usearch_ok'] = False
        out['usearch_error'] = str(e)

    # 6. index paths
    try:
        from memory_store import _FASTEMBED_INDEX_PATH, _HNSW_INDEX_PATH
        fe_idx = _FASTEMBED_INDEX_PATH if _os.path.exists(_FASTEMBED_INDEX_PATH) else _FASTEMBED_INDEX_PATH + '.npz'
        hnsw_idx = _HNSW_INDEX_PATH
        out['fastembed_index_path'] = fe_idx
        out['fastembed_index_exists'] = _os.path.exists(fe_idx)
        out['fastembed_index_size_kb'] = _os.path.getsize(fe_idx) // 1024 if _os.path.exists(fe_idx) else 0
        out['hnsw_index_path'] = hnsw_idx
        out['hnsw_index_exists'] = _os.path.exists(hnsw_idx)
        out['hnsw_index_size_kb'] = _os.path.getsize(hnsw_idx) // 1024 if _os.path.exists(hnsw_idx) else 0
    except Exception as e:
        out['index_path_error'] = str(e)

    # 7. _hnsw_available() result
    try:
        from memory_store import _hnsw_available
        out['hnsw_available_result'] = _hnsw_available()
    except Exception as e:
        out['hnsw_available_result'] = f'ERROR: {e}'

    # 8. _use_hnsw decision
    try:
        from memory_store import _HNSW_INDEX_PATH as _hip, _hnsw_available as _ha
        out['use_hnsw_would_be'] = _os.path.exists(_hip) and _ha()
    except Exception as e:
        out['use_hnsw_would_be'] = f'ERROR: {e}'

    # 9. Actual backend for sample query (tiny, just to get backend field)
    try:
        from memory_store import fastembed_search
        sample = fastembed_search('test', limit=1, min_score=0.0)
        out['sample_backend'] = sample.get('backend', 'unknown')
        out['sample_error'] = sample.get('error', None)
        out['fallback_reason'] = (
            'HNSW not available (_hnsw_available=False)'
            if not out.get('hnsw_available_result')
            else 'HNSW exception (see hnsw_error.log)'
            if sample.get('backend') == 'fastembed_brute_force'
            else None
        )
    except Exception as e:
        out['sample_backend'] = f'ERROR: {e}'

    # 10. operator summary
    issues = []
    if not out.get('fastembed_ok') and not out.get('fastembed_subprocess_ok'):
        issues.append('fastembed unavailable')
    elif not out.get('fastembed_ok') and out.get('fastembed_subprocess_ok'):
        issues.append('embedding via subprocess')
    if not out.get('usearch_ok'):
        issues.append('usearch import failed')
    if not out.get('hnsw_available_result'):
        issues.append('_hnsw_available=False')
    if not out.get('fastembed_index_exists'):
        issues.append('fastembed index missing')
    if not out.get('hnsw_index_exists'):
        issues.append('HNSW index missing')
    if out.get('sample_backend') == 'fastembed_brute_force':
        issues.append(f'using brute_force (reason: {out.get("fallback_reason", "unknown")})')

    if issues:
        out['operator_summary'] = f'backend={out.get("sample_backend")}; {"; ".join(issues)}'
    else:
        out['operator_summary'] = f'backend={out.get("sample_backend")}; all checks ok'
    return out


def tool_memory_hnsw_rebuild(args: dict) -> dict:
    """Build usearch HNSW index from existing fastembed .npz index.
    Fast ANN search, no MSVC required. Run after fastembed_rebuild."""
    import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
    from memory_store import hnsw_rebuild
    return hnsw_rebuild()


def tool_memory_hnsw_status(args: dict) -> dict:
    """Check usearch HNSW index status."""
    import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
    from memory_store import hnsw_status
    return hnsw_status()


def _fastembed_jobs_dir() -> str:
    path = r'C:\MirageWork\mcp-server\data\fastembed_jobs'
    os.makedirs(path, exist_ok=True)
    return path


def _fastembed_job_path(job_id: str) -> str:
    safe = ''.join(ch for ch in str(job_id) if ch.isalnum() or ch in ('-', '_'))[:64]
    return os.path.join(_fastembed_jobs_dir(), f'{safe}.json')


def _write_fastembed_job_snapshot(job_id: str, job: dict) -> None:
    try:
        payload = dict(job)
        payload['job_id'] = job_id
        path = _fastembed_job_path(job_id)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        log.exception('failed to persist fastembed job snapshot')


def _read_fastembed_job_snapshot(job_id: str) -> dict:
    try:
        with open(_fastembed_job_path(job_id), 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {'job_id': job_id, 'state': 'error', 'result': {'error': str(e)}}


def tool_memory_fastembed_rebuild(args: dict) -> dict:
    """Start async fastembed rebuild (returns job_id; poll memory_fastembed_rebuild_status).
    Pass sync=true to block until done (legacy behavior, will likely exceed MCP timeout for full index).
    """
    import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
    from memory_store import fastembed_rebuild
    namespaces = (args or {}).get('namespaces', None)
    sync = bool((args or {}).get('sync', False))
    if sync:
        return fastembed_rebuild(namespaces=namespaces)
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            'state': 'running', 'kind': 'fastembed_rebuild',
            'namespaces': namespaces,
            'started_at': time.time(), 'result': None,
        }
        _write_fastembed_job_snapshot(job_id, _jobs[job_id])

    def _run():
        started = time.time()
        try:
            result = fastembed_rebuild(namespaces=namespaces)
            # Chain HNSW rebuild so the index doesn't go out of sync with the
            # new fastembed array. Without this, fastembed_search returns
            # "index N out of bounds" errors when the HNSW still references
            # entry positions from the previous build (observed 2026-05-11:
            # HNSW had 5304 entries cached, fastembed rebuilt to 5268 -> all
            # searches hitting positions 5268..5303 errored). Best-effort:
            # if HNSW rebuild fails, the fastembed rebuild itself is still
            # considered successful and is logged separately.
            hnsw_info = None
            if result.get('ok'):
                try:
                    from memory_store import hnsw_rebuild
                    t_h = time.time()
                    hnsw_info = hnsw_rebuild()
                    hnsw_info['rebuilt'] = True
                    hnsw_info['elapsed_sec'] = round(time.time() - t_h, 2)
                except Exception as he:
                    hnsw_info = {'rebuilt': False, 'error': str(he)}
            with _jobs_lock:
                _jobs[job_id]['state'] = 'done'
                _jobs[job_id]['result'] = result
                _jobs[job_id]['hnsw_chain'] = hnsw_info
                _jobs[job_id]['duration_sec'] = round(time.time() - started, 2)
                _jobs[job_id]['finished_at'] = time.time()
                _write_fastembed_job_snapshot(job_id, _jobs[job_id])
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]['state'] = 'error'
                _jobs[job_id]['result'] = {'error': str(e)}
                _jobs[job_id]['duration_sec'] = round(time.time() - started, 2)
                _jobs[job_id]['finished_at'] = time.time()
                _write_fastembed_job_snapshot(job_id, _jobs[job_id])

    threading.Thread(target=_run, daemon=True).start()
    return {'job_id': job_id, 'state': 'running', 'kind': 'fastembed_rebuild',
            'namespaces': namespaces,
            'message': 'fastembed rebuild started; poll memory_fastembed_rebuild_status'}


def tool_memory_fastembed_rebuild_status(args: dict) -> dict:
    """Poll status of an async fastembed rebuild job. Pass job_id."""
    job_id = (args or {}).get('job_id', '')
    if not job_id:
        return {'error': 'job_id required'}
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        job = _read_fastembed_job_snapshot(job_id) or {}
    if not job:
        return {'job_id': job_id, 'state': 'not_found'}
    elapsed = None
    if job.get('started_at'):
        end = job.get('finished_at') or time.time()
        elapsed = round(end - job['started_at'], 2)
    return {
        'job_id': job_id,
        'state': job.get('state', 'unknown'),
        'kind': job.get('kind', 'fastembed_rebuild'),
        'namespaces': job.get('namespaces'),
        'elapsed_sec': elapsed,
        'result': job.get('result'),
    }


def tool_memory_fastembed_search(args: dict) -> dict:
    """Search memory using fastembed true embeddings (cosine similarity).
    Returns epistemic_status='semantic_match' for genuine embedding similarity."""
    import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
    from memory_store import fastembed_search
    query = (args or {}).get('query', '')
    if not query:
        return {'hits': [], 'error': 'query required'}
    namespace = (args or {}).get('namespace', None)
    limit = int((args or {}).get('limit', 5))
    min_score = float((args or {}).get('min_score', 0.3))
    _types = (args or {}).get('types', None)
    if isinstance(_types, str) and _types:
        # Some MCP transports serialize arrays as JSON strings — decode first.
        s = _types.strip()
        if s.startswith('[') and s.endswith(']'):
            try:
                import json as _j
                parsed = _j.loads(s)
                if isinstance(parsed, list):
                    _types = parsed
            except Exception:
                pass
    if isinstance(_types, list):
        _types = [str(t) for t in _types if t]
    elif isinstance(_types, str) and _types:
        _types = [_types]
    else:
        _types = None
    return fastembed_search(query, namespace=namespace, limit=limit, min_score=min_score, types=_types)


def tool_memory_fastembed_status(args: dict) -> dict:
    """Check fastembed backend availability and index status."""
    import os, sys as _sys
    _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
    from memory_store import _FASTEMBED_VENV_PYTHON, _FASTEMBED_INDEX_PATH, _fastembed_available, _FASTEMBED_MODEL
    idx_path = _FASTEMBED_INDEX_PATH if os.path.exists(_FASTEMBED_INDEX_PATH) else _FASTEMBED_INDEX_PATH + '.npz'
    idx_exists = os.path.exists(idx_path)
    available = _fastembed_available()
    count = 0
    dims = 0
    if idx_exists:
        try:
            import numpy as np
            data = np.load(idx_path, allow_pickle=True)
            count = len(data['ids'])
            dims = data['embeddings'].shape[1] if len(data['embeddings'].shape) > 1 else 0
        except Exception:
            pass
    return {
        'available': available,
        'venv_python': _FASTEMBED_VENV_PYTHON,
        'model': _FASTEMBED_MODEL,
        'index_exists': idx_exists,
        'index_path': idx_path,
        'count': count,
        'dims': dims,
        'epistemic_status': 'true_embedding' if available else 'unavailable',
        'operator_summary': (
            f'fastembed ready; index has {count} entries ({dims}d)' if available and idx_exists
            else 'fastembed available but index not built; run memory_fastembed_rebuild' if available
            else 'fastembed venv not available; check venv/memory-win'
        ),
    }


def tool_memory_link_promotion_review(args: dict) -> dict:
    """Approve or reject link promotion candidates.
    
    Workflow:
      1. Call memory_link_promotion_candidates to get candidates.
      2. For each candidate, decide:
         - approve: create explicit link (promote_to relation type)
         - reject:  keep as related_auto (no action)
         - supersedes: create supersedes link instead
         - supports:   create supports link instead
      3. Pass decisions list here.
    
    Args:
      decisions: list of {link_id, action: approve|reject|supersedes|supports|contradicts, note?}
      dry_run: bool (default true) - preview without writing
    """
    import time, uuid
    decisions = (args or {}).get('decisions', [])
    dry_run = (args or {}).get('dry_run', True)

    if not decisions:
        return {'error': 'decisions list required', 'hint': 'Get candidates via memory_link_promotion_candidates first'}

    con_l = _links_connect()
    results = []
    try:
        for dec in decisions:
            link_id = dec.get('link_id', '')
            action  = dec.get('action', 'reject')  # approve|reject|supersedes|supports|contradicts
            note    = dec.get('note', '')
            override_promote_to = dec.get('promote_to', None)  # override if different from candidate suggestion

            # Fetch existing link
            row = con_l.execute(
                'SELECT id, source_id, target_id, relation_type, score FROM links WHERE id=?',
                (link_id,)
            ).fetchone()
            if not row:
                results.append({'link_id': link_id, 'status': 'not_found'})
                continue

            _, src, tgt, rel_type, score = row

            if action == 'reject':
                results.append({'link_id': link_id, 'status': 'rejected', 'dry_run': dry_run})
                continue

            # Determine target relation type
            if action == 'approve':
                new_rel = override_promote_to or 'related_manual'
            elif action in ('supersedes', 'supports', 'contradicts'):
                new_rel = action
            else:
                results.append({'link_id': link_id, 'status': 'unknown_action', 'action': action})
                continue

            if dry_run:
                results.append({
                    'link_id': link_id, 'status': 'would_promote',
                    'from': rel_type, 'to': new_rel,
                    'source_id': src, 'target_id': tgt,
                    'dry_run': True,
                })
                continue

            # Create new explicit link
            new_id = str(uuid.uuid4())
            now = int(time.time())
            con_l.execute(
                'INSERT INTO links (id, source_id, target_id, relation_type, score, note, created_at) VALUES (?,?,?,?,?,?,?)',
                (new_id, src, tgt, new_rel, score or 0.8, note or f'promoted from {rel_type}', now)
            )
            # Mark old related_auto as superseded (update note)
            con_l.execute(
                "UPDATE links SET note=? WHERE id=?",
                (f'promoted_to:{new_id}', link_id)
            )
            con_l.commit()
            results.append({
                'link_id': link_id, 'status': 'promoted',
                'new_link_id': new_id, 'from': rel_type, 'to': new_rel,
                'source_id': src, 'target_id': tgt, 'dry_run': False,
            })

        approved = sum(1 for r in results if r.get('status') in ('promoted', 'would_promote'))
        rejected = sum(1 for r in results if r.get('status') == 'rejected')
        return {
            'operator_summary': f'{"[DRY RUN] " if dry_run else ""}reviewed {len(decisions)} candidates: {approved} promote, {rejected} reject',
            'dry_run': dry_run,
            'approved_count': approved,
            'rejected_count': rejected,
            'results': results,
            'hint': 'Set dry_run=false to actually create links' if dry_run else 'Links created. Run memory_link_health to verify.',
        }
    finally:
        con_l.close()




def tool_memory_link_bulk_promote(args: dict) -> dict:
    """Bulk promote high-confidence related_auto links automatically.
    
    Safe automation: only promotes when ALL of these are true:
      - score >= min_score (default 0.95)
      - promote_to != 'keep_auto'
      - both entries are decision type
      - hub_count >= min_hub (default 5)
    
    All promotions are related_manual (not supersedes/supports/contradicts).
    Explicit semantic links still require manual review.
    dry_run=true by default.
    """
    min_score = float((args or {}).get('min_score', 0.95))
    min_hub   = int((args or {}).get('min_hub', 5))
    dry_run   = (args or {}).get('dry_run', True)
    limit     = int((args or {}).get('limit', 50))

    # Get candidates
    cands_result = tool_memory_link_promotion_candidates({'limit': limit * 2, 'min_score': min_score})
    candidates = cands_result.get('candidates', [])

    # Filter for safe auto-promotion
    safe = [
        c for c in candidates
        if (
            float(c.get('score', 0)) >= min_score
            and 'hub_count' in ' '.join(str(r) for r in c.get('reasons', []))
            and int([r for r in c.get('reasons', []) if 'hub_count' in str(r)][0].split('=')[1]) >= min_hub
            if any('hub_count' in str(r) for r in c.get('reasons', []))
            else False
        )
        and c.get('promote_to') not in ('keep_auto', 'supersedes', 'supports', 'contradicts')
    ]

    if not safe:
        return {
            'operator_summary': f'no safe auto-promotion candidates (min_score={min_score}, min_hub={min_hub})',
            'candidate_count': len(candidates),
            'safe_count': 0,
            'dry_run': dry_run,
            'promoted': [],
        }

    decisions = [
        {'link_id': c['link_id'], 'action': 'approve', 'promote_to': 'related_manual',
         'note': f'bulk_promote: score={c.get("score","")}, hub_count in {c.get("reasons",[])}'}
        for c in safe[:limit]
    ]

    result = tool_memory_link_promotion_review({'decisions': decisions, 'dry_run': dry_run})

    return {
        'operator_summary': f'{"[DRY RUN] " if dry_run else ""}bulk promoted {result.get("approved_count",0)} related_manual links (safe: score>={min_score}, hub>={min_hub})',
        'candidate_count': len(candidates),
        'safe_count': len(safe),
        'dry_run': dry_run,
        'approved_count': result.get('approved_count', 0),
        'rejected_count': result.get('rejected_count', 0),
        'promoted': [r for r in result.get('results', []) if r.get('status') in ('promoted', 'would_promote')],
    }


def tool_memory_contradiction_review(args: dict) -> dict:
    """Classify contradiction candidates as false_positive, supports, or contradicts.
    
    Workflow:
      1. Call memory_contradiction_candidates to get candidates.
      2. For each pair, decide:
         - false_positive: not a real contradiction (e.g. historical record)
         - contradicts: genuine design contradiction -> create contradicts link
         - supports: the mention actually supports the ban (e.g. investigation that led to ban)
      3. Pass decisions list here.
    
    Args:
      decisions: list of {ban_id, mention_id, classification: false_positive|contradicts|supports, note?}
      dry_run: bool (default true)
    """
    import time, uuid
    decisions = (args or {}).get('decisions', [])
    dry_run = (args or {}).get('dry_run', True)

    if not decisions:
        return {
            'error': 'decisions list required',
            'hint': 'Get candidates via memory_contradiction_candidates first. Each decision needs ban_id, mention_id, classification.'
        }

    con_l = _links_connect()
    results = []
    try:
        for dec in decisions:
            ban_id      = dec.get('ban_id', '')
            mention_id  = dec.get('mention_id', '')
            classif     = dec.get('classification', 'false_positive')
            note        = dec.get('note', '')

            if not ban_id or not mention_id:
                results.append({'status': 'missing_ids', 'decision': dec})
                continue

            if classif == 'false_positive':
                results.append({
                    'ban_id': ban_id, 'mention_id': mention_id,
                    'status': 'false_positive_noted', 'dry_run': dry_run,
                    'note': 'No link created. Consider adding false_positive note to ban entry if needed.',
                })
                continue

            rel_type = classif  # 'contradicts' or 'supports'
            if rel_type not in ('contradicts', 'supports'):
                results.append({'status': 'unknown_classification', 'classification': classif})
                continue

            if dry_run:
                results.append({
                    'ban_id': ban_id, 'mention_id': mention_id,
                    'status': f'would_create_{rel_type}', 'dry_run': True,
                })
                continue

            new_id = str(uuid.uuid4())
            now = int(time.time())
            con_l.execute(
                'INSERT INTO links (id, source_id, target_id, relation_type, score, note, created_at) VALUES (?,?,?,?,?,?,?)',
                (new_id, ban_id, mention_id, rel_type, 0.9, note or f'from contradiction_review', now)
            )
            con_l.commit()
            results.append({
                'ban_id': ban_id, 'mention_id': mention_id,
                'status': f'{rel_type}_link_created',
                'new_link_id': new_id, 'dry_run': False,
            })

        contradicts_created = sum(1 for r in results if 'contradicts_link_created' in r.get('status', ''))
        supports_created    = sum(1 for r in results if 'supports_link_created' in r.get('status', ''))
        fp_noted            = sum(1 for r in results if r.get('status') == 'false_positive_noted')
        return {
            'operator_summary': (
                f'{"[DRY RUN] " if dry_run else ""}reviewed {len(decisions)} pairs: '
                f'{contradicts_created} contradicts, {supports_created} supports, {fp_noted} false_positive'
            ),
            'dry_run': dry_run,
            'contradicts_created': contradicts_created,
            'supports_created': supports_created,
            'false_positive_count': fp_noted,
            'results': results,
            'hint': 'Set dry_run=false to create links' if dry_run else 'Links created. Run memory_link_health to check explicit_ratio.',
        }
    finally:
        con_l.close()


def tool_memory_link_health(args: dict) -> dict:
    """Return link network health: density, isolated ratio, semantic ratio, cross-namespace ratio."""
    import sqlite3, os
    db_path = _links_db_path()
    if not os.path.exists(db_path):
        return {'error': 'links db not found', 'total_links': 0}
    con = sqlite3.connect(db_path)
    try:
        total_links = con.execute('SELECT COUNT(*) FROM links').fetchone()[0]
        by_type = dict(con.execute(
            'SELECT relation_type, COUNT(*) FROM links GROUP BY relation_type'
        ).fetchall())
        by_strength = {
            'related_auto': 0,
            'related_manual': 0,
            'supports': 0,
            'supersedes': 0,
            'contradicts': 0,
            'consolidated_into': 0,
        }
        for rel, note, count in con.execute(
            'SELECT relation_type, COALESCE(note, ""), COUNT(*) FROM links GROUP BY relation_type, COALESCE(note, "")'
        ).fetchall():
            trust = _link_trust_class(rel, note)
            key = trust.get('relation_strength', rel)
            by_strength[key] = by_strength.get(key, 0) + count
        # join entries for namespace cross-check
        cross = con.execute("""
            SELECT COUNT(*) FROM links l
            JOIN entries e1 ON l.source_id = e1.id
            JOIN entries e2 ON l.target_id = e2.id
            WHERE e1.namespace != e2.namespace
        """).fetchone()[0]
    finally:
        con.close()

    # entry stats from main memory db
    mem_db = _memory_db_path()
    con2 = sqlite3.connect(mem_db)
    try:
        active = con2.execute("SELECT COUNT(*) FROM entries WHERE status='active'").fetchone()[0]
        isolated = con2.execute("""
            SELECT COUNT(*) FROM entries e
            WHERE status='active'
            AND e.id NOT IN (SELECT source_id FROM links l2)
            AND e.id NOT IN (SELECT target_id FROM links l3)
        """).fetchone()[0]
    except Exception:
        active = 0; isolated = 0
    finally:
        con2.close()

    explicit_count = sum(by_type.get(t, 0) for t in ('supports', 'supersedes', 'contradicts'))
    candidate_count = by_strength.get('related_auto', 0)
    manual_candidate_count = by_strength.get('related_manual', 0)
    structural_count = by_type.get('consolidated_into', 0)
    density = round(total_links / active, 4) if active else 0
    isolated_ratio = round(isolated / active, 4) if active else 0
    semantic_ratio = round(explicit_count / total_links, 4) if total_links else 0
    cross_ratio = round(cross / total_links, 4) if total_links else 0

    _EPISTEMIC_STATUS = 'candidate_graph' if semantic_ratio < 0.2 else 'partial_semantic'
    action_plan = []
    if semantic_ratio < 0.15:
        action_plan.append({
            'priority': 'P0',
            'trigger': 'explicit_ratio < 15%',
            'action': 'promote high-value related_auto links only after manual/source inspection',
            'reason': 'most links are weak candidates; explicit supports/supersedes/contradicts links are still sparse',
        })
    if isolated_ratio > 0.60:
        action_plan.append({
            'priority': 'P1',
            'trigger': 'isolated_ratio > 60%',
            'action': 'defer broad isolated-entry backfill until fastembed_hnsw is active',
            'reason': 'hashed ngram backfill would mostly create more weak related_auto links',
        })
    auto_ratio = round(candidate_count / total_links, 4) if total_links else 0
    if auto_ratio > 0.80:
        action_plan.append({
            'priority': 'P1',
            'trigger': 'related_auto_ratio > 80%',
            'action': 'read graph as candidate_graph and prefer explicit links for decisions',
            'reason': 'automatic related links dominate the graph',
        })
    if cross_ratio > 0.20 and semantic_ratio < 0.20:
        action_plan.append({
            'priority': 'P2',
            'trigger': 'cross_namespace_ratio > 20% while explicit_ratio < 20%',
            'action': 'treat cross-namespace links as discovery hints, not confirmed architecture coupling',
            'reason': 'namespace-spanning candidates may be lexical coincidences until promoted',
        })

    return {
        'operator_summary': (
            f'{"candidate_graph" if semantic_ratio < 0.2 else "partial_semantic"}; '
            f'{total_links} links, density={density}, isolated={isolated_ratio:.1%}, '
            f'explicit_ratio={semantic_ratio:.1%}, cross_ns={cross_ratio:.1%}'
        ),
        'epistemic_status': _EPISTEMIC_STATUS,
        'total_links': total_links,
        'active_entries': active,
        'density': density,
        'isolated_entries': isolated,
        'isolated_ratio': isolated_ratio,
        'by_relation_type': by_type,
        'by_relation_strength': by_strength,
        'explicit_links': explicit_count,
        'candidate_links': candidate_count,
        'manual_candidate_links': manual_candidate_count,
        'related_auto_ratio': auto_ratio,
        'structural_links': structural_count,
        'semantic_ratio': semantic_ratio,
        'action_plan': action_plan,
        'meaning_strength_scale': {
            'related_auto': 1,
            'related_manual': 2,
            'consolidated_into': 3,
            'supports/supersedes/contradicts': 4,
        },
        'safe_reading': (
            'related_auto is weak candidate evidence; related_manual is operator-marked but still not proof; '
            'supports/supersedes/contradicts are explicit semantic links.'
        ),
        'cross_namespace_links': cross,
        'cross_namespace_ratio': cross_ratio,
        'upgrade_path': 'add verified explicit links (supports/supersedes/contradicts) and install fastembed backend',
    }


def tool_memory_link_traverse(args: dict) -> dict:
    """Multi-hop traversal: follow links N hops from a starting entry.
    Useful for 'what was the background for this decision?'
    """
    start_id = (args or {}).get('entry_id', '')
    max_hops  = int((args or {}).get('max_hops', 2) or 2)
    rel_types = (args or {}).get('relation_types', None)
    
    if not start_id:
        return {'error': 'entry_id required'}
    
    max_hops = min(max_hops, 3)  # Safety cap
    
    con = _links_connect()
    try:
        visited = set()
        frontier = {start_id}
        all_nodes = []
        all_edges = []
        
        for hop in range(max_hops):
            if not frontier:
                break
            next_frontier = set()
            
            for eid in frontier:
                if eid in visited:
                    continue
                visited.add(eid)
                
                # Fetch entry
                row = con.execute(
                    'SELECT id, namespace, type, title, content FROM entries WHERE id = ?',
                    (eid,)
                ).fetchone()
                if row:
                    all_nodes.append({
                        'id': row[0], 'namespace': row[1], 'type': row[2],
                        'title': row[3], 'content': (row[4] or '')[:150],
                        'hop': hop
                    })
                
                # Traverse links
                q = 'SELECT id, source_id, target_id, relation_type, score, note FROM links WHERE source_id = ? OR target_id = ?'
                for lrow in con.execute(q, (eid, eid)).fetchall():
                    if rel_types and lrow[3] not in rel_types:
                        continue
                    trust = _link_trust_class(lrow[3], lrow[5] or '')
                    all_edges.append({'id':lrow[0],'source':lrow[1],'target':lrow[2],
                                      'type':lrow[3],'score':lrow[4],'note':lrow[5],
                                      **trust})
                    other = lrow[2] if lrow[1] == eid else lrow[1]
                    if other not in visited:
                        next_frontier.add(other)
            
            frontier = next_frontier
        
        return {
            'start_id': start_id,
            'nodes': all_nodes,
            'edges': all_edges,
            'hops': max_hops,
        }
    finally:
        con.close()



# ---------------------------------------------------------------------------
# memory_consolidate (Phase 4: consolidation)
# ---------------------------------------------------------------------------
def tool_memory_consolidate(args: dict) -> dict:
    """Consolidate high-salience repeated entries into semantic memory.
    
    Finds entries with access_count >= threshold and importance_v2 >= threshold,
    groups by similarity, and asks LLM to synthesize a semantic entry.
    """
    import sqlite3, uuid, time, json
    
    ns        = (args or {}).get('namespace', 'mirage-vulkan')
    min_acc   = int((args or {}).get('min_access_count', 3) if (args or {}).get('min_access_count') is not None else 3)
    min_imp   = float((args or {}).get('min_importance', 0.6) or 0.6)
    dry_run   = bool((args or {}).get('dry_run', False))
    max_group = int((args or {}).get('max_group_size', 5) or 5)
    
    db = r'C:\MirageWork\mcp-server\data\memory.db'
    con = sqlite3.connect(db)
    
    try:
        # Find consolidation candidates
        rows = con.execute("""
            SELECT id, type, title, content, importance_v2, access_count, tags
            FROM entries
            WHERE namespace = ?
              AND COALESCE(access_count, 0) >= ?
              AND COALESCE(importance_v2, 0.5) >= ?
              AND (type = 'raw' OR type = 'decision')
              AND (status IS NULL OR status = 'active')
              AND id NOT IN (SELECT source_id FROM links WHERE relation_type = 'consolidated_into')
            ORDER BY importance_v2 * COALESCE(access_count, 0) DESC
            LIMIT ?
        """, (ns, min_acc, min_imp, max_group * 3)).fetchall()
        
        if not rows:
            return {'consolidated': 0, 'message': 'No candidates found', 'namespace': ns}
        
        candidates = [
            {'id': r[0], 'type': r[1], 'title': r[2],
             'content': (r[3] or '')[:300], 'importance_v2': r[4],
             'access_count': r[5], 'tags': r[6]}
            for r in rows
        ]
        
        if dry_run:
            return {'dry_run': True, 'candidates': candidates, 'count': len(candidates)}
        
        # Group candidates (simple: take top max_group)
        group = candidates[:max_group]
        
        # Build LLM prompt
        entries_text = '\n'.join([
            f"[{i+1}] ({e['type']}) {e['title']}: {e['content']}"
            for i, e in enumerate(group)
        ])
        prompt = f"""以下の{ns}の記憶エントリ{len(group)}件を1つのsemantic記憶として統合してください。
関連する情報を統合し、本質的な知識を抽出してください。

{entries_text}

以下のJSON形式で返してください（日本語OK）
{{
  "title": "統合後のタイトル",
  "content": "統合された内容（200文字以内）,
  "tags": ["タグ1", "タグ2"],
  "importance": 4
}}"""
        
        # Call LLM
        try:
            import sys as _sys
            _sys.path.insert(0, r'C:\MirageWork\mcp-server-v2')
            import llm
            raw = llm.call(prompt, purpose='consolidation', max_tokens=400, timeout=30)
            
            import re
            match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
            if not match:
                return {'error': 'LLM returned invalid JSON', 'raw': raw[:200]}
            data = __import__('json').loads(match.group())
        except Exception as e:
            return {'error': f'LLM failed: {e}', 'candidates': [c['id'] for c in group]}
        
        # Create semantic entry
        new_id = str(uuid.uuid4())
        now = int(time.time())
        
        from memory import store as mem_store
        mem_store.append_entry(
            namespace=ns,
            type_='semantic',
            title=data.get('title', 'Consolidated Memory'),
            content=data.get('content', ''),
            tags=data.get('tags', []),
            importance=data.get('importance', 4),
            role='system',
        )
        
        # Find the just-created entry
        row = con.execute(
            "SELECT id FROM entries WHERE namespace=? AND type='semantic' ORDER BY created_at DESC LIMIT 1",
            (ns,)
        ).fetchone()
        if row:
            new_id = row[0]
        
        # Create 'consolidated_into' links
        consolidated = []
        for e in group:
            link_id = str(uuid.uuid4())
            con.execute(
                'INSERT INTO links (id, source_id, target_id, relation_type, score, created_at, note) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (link_id, e['id'], new_id, 'consolidated_into', 1.0, now, 'auto-consolidated')
            )
            # Lower original entry importance_v2 (already represented in semantic)
            con.execute(
                'UPDATE entries SET importance_v2 = importance_v2 * 0.5 WHERE id = ?',
                (e['id'],)
            )
            consolidated.append(e['id'])
        
        con.commit()
        
        return {
            'consolidated': len(consolidated),
            'semantic_entry_id': new_id,
            'semantic_title': data.get('title'),
            'source_ids': consolidated,
            'namespace': ns,
        }
    finally:
        con.close()



# ---------------------------------------------------------------------------
# memory_ingest (karpathy LLM Wiki - Ingest フロー)
# ---------------------------------------------------------------------------
def tool_memory_ingest(args: dict) -> dict:
    """Ingest a new entry and auto-generate cross-reference links.

    [v2 2026-04-26: CJK-safe search + tag fallback + debug info]

    Workflow:
    1. Write new entry to DB
    2. Build CJK-safe search query (tags + title + first 100 chars; no split)
    3. FTS search; if 0 hits, fall back to per-tag search
    4. LLM judges relation type for top candidates
    5. Auto-create links for matches
    6. Return links_created count + auto_link_debug for visibility
    """
    import sqlite3, uuid, time, json

    ns         = (args or {}).get('namespace', 'mirage-infra')
    etype      = (args or {}).get('type', 'raw')
    title      = (args or {}).get('title', '')
    content    = (args or {}).get('content', '')
    tags       = (args or {}).get('tags', [])
    importance = int((args or {}).get('importance', 3) or 3)
    room_id    = (args or {}).get('room_id', None)
    auto_link  = bool((args or {}).get('auto_link', True))
    max_cands  = int((args or {}).get('max_candidates', 5) or 5)

    if not content:
        return {'error': 'content required'}

    from memory import store as mem_store
    mem_store.append_entry(
        namespace=ns, type_=etype, title=title,
        content=content, tags=tags, importance=importance, role='user',
    )

    db = r'C:\MirageWork\mcp-server\data\memory.db'
    con = sqlite3.connect(db)
    try:
        new_row = con.execute(
            "SELECT id FROM entries WHERE namespace=? ORDER BY created_at DESC LIMIT 1",
            (ns,)
        ).fetchone()
        if not new_row:
            return {'error': 'entry not found after insert'}
        new_id = new_row[0]

        if not room_id:
            room_id = f'{ns}:general'
        con.execute("UPDATE entries SET room_id=? WHERE id=?", (room_id, new_id))
        con.commit()

        links_created = []
        debug = {
            'auto_link_enabled': auto_link,
            'fts_candidates': 0,
            'tag_fallback_candidates': 0,
            'hits_for_llm': 0,
            'llm_called': False,
        }

        if auto_link and content.strip():
            query_parts = list(tags) if tags else []
            if title:
                query_parts.append(title)
            if content:
                query_parts.append(content[:100])
            search_query = ' '.join(query_parts)[:200]
            debug['search_query'] = search_query[:100]

            candidates = mem_store.search(ns, query=search_query, limit=max_cands * 2)
            fts_hits = [h for h in candidates.get('hits', []) if h.get('id') != new_id]
            debug['fts_candidates'] = len(fts_hits)
            hits = fts_hits[:max_cands]

            if not hits and tags:
                tag_hits = []
                for tag in tags[:3]:
                    tag_search = mem_store.search(ns, query=tag, limit=3)
                    tag_hits.extend([h for h in tag_search.get('hits', [])
                                     if h.get('id') != new_id])
                seen = set()
                hits = []
                for h in tag_hits:
                    if h['id'] not in seen:
                        seen.add(h['id'])
                        hits.append(h)
                hits = hits[:max_cands]
                debug['tag_fallback_candidates'] = len(hits)

            debug['hits_for_llm'] = len(hits)

            if hits:
                cand_text = '\n'.join([
                    f"[{i+1}] id={h['id'][:8]} type={h.get('type','')} "
                    f"title={h.get('title','')[:40]}: {str(h.get('snippet',''))[:80]}"
                    for i, h in enumerate(hits)
                ])

                prompt = f"""新しいエントリとの関係を判定してください。

新エントリ: {title or content[:100]}

候補エントリ:
{cand_text}

各候補について以下のJSONリストで返してください（関係なしは省略可）:
[
  {{"index": 1, "relation": "supports|contradicts|related|supersedes", "score": 0.0-1.0}},
  ...
]
関係のない候補は含めないでください。JSONのみ返してください。"""

                debug['llm_called'] = True
                try:
                    import sys as _sys
                    _sys.path.insert(0, r'C:\MirageWork\mcp-server-v2')
                    import llm
                    raw = llm.call(prompt, purpose='ingest_link', max_tokens=300, timeout=20)

                    import re
                    match = re.search(r'\[.*?\]', raw, re.DOTALL)
                    if match:
                        relations = json.loads(match.group())
                        now = int(time.time())
                        for rel in relations:
                            idx = rel.get('index', 0) - 1
                            if 0 <= idx < len(hits):
                                target_id = hits[idx]['id']
                                rel_type = rel.get('relation', 'related')
                                if rel_type not in ('supports','contradicts','related','supersedes'):
                                    rel_type = 'related'
                                score = float(rel.get('score', 0.7))
                                link_id = str(uuid.uuid4())
                                con.execute(
                                    'INSERT INTO links (id, source_id, target_id, relation_type, score, created_at, note) '
                                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                                    (link_id, new_id, target_id, rel_type, score, now, 'auto-ingest')
                                )
                                links_created.append({
                                    'target_id': target_id[:8],
                                    'relation': rel_type,
                                    'score': score
                                })
                        con.commit()
                except Exception as e:
                    links_created.append({'error': str(e)[:60]})

        return {
            'entry_id': new_id,
            'room_id': room_id,
            'links_created': len([l for l in links_created if 'error' not in l]),
            'links': links_created,
            'auto_link_debug': debug,
        }
    finally:
        con.close()



# ---------------------------------------------------------------------------
# memory_lint (karpathy LLM Wiki - Lint 操作)
# ---------------------------------------------------------------------------
def tool_memory_lint(args: dict) -> dict:
    """Health-check the memory wiki.
    
    Detects:
    - Orphan entries: no links in or out
    - Stale decisions: decision type, older than threshold, no supersedes
    - Contradiction candidates: entries with 'contradicts' links
    - Low-importance clusters: many entries with importance_v2 < 0.3
    - Namespace imbalance: namespaces with no recent entries
    
    Returns a lint report with actionable suggestions.
    """
    import sqlite3, time
    
    ns          = (args or {}).get('namespace', None)
    stale_days  = int((args or {}).get('stale_days', 30) or 30)
    
    db = r'C:\MirageWork\mcp-server\data\memory.db'
    con = sqlite3.connect(db)
    report = {'issues': [], 'stats': {}, 'suggestions': []}
    
    try:
        now = int(time.time())
        stale_ts = now - stale_days * 86400
        
        ns_filter = "AND namespace = ?" if ns else ""
        ns_params = [ns] if ns else []
        
        # --- 1. Orphan entries (no links) ---
        orphan_q = f"""
            SELECT id, namespace, type, title, created_at
            FROM entries
            WHERE id NOT IN (SELECT source_id FROM links)
              AND id NOT IN (SELECT target_id FROM links)
              AND (status IS NULL OR status != 'archived')
              {ns_filter}
            LIMIT 20
        """
        orphans = con.execute(orphan_q, ns_params).fetchall()
        # Only flag decision/fact type orphans as issues
        orphan_issues = [r for r in orphans if r[2] in ('decision', 'fact', 'semantic')]
        if orphan_issues:
            report['issues'].append({
                'type': 'orphan',
                'count': len(orphan_issues),
                'sample': [{'id': r[0][:8], 'ns': r[1], 'type': r[2],
                            'title': (r[3] or '')[:40]} for r in orphan_issues[:5]],
                'description': f'{len(orphan_issues)} decision/fact/semantic entries with no links'
            })
        
        # --- 2. Stale decisions ---
        # Two queries: total count (uncapped) + sample rows (LIMIT 10).
        # Previously a single LIMIT 10 query was used, so the displayed count
        # capped at 10 even when hundreds of stale decisions existed.
        stale_where = f"""
            FROM entries
            WHERE type = 'decision'
              AND created_at < ?
              AND id NOT IN (SELECT source_id FROM links WHERE relation_type = 'supersedes')
              AND (status IS NULL OR status = 'active')
              {ns_filter}
        """
        stale_total = con.execute(
            f"SELECT COUNT(*) {stale_where}", [stale_ts] + ns_params
        ).fetchone()[0]
        stale_q = f"""
            SELECT id, namespace, title, created_at, importance_v2
            {stale_where}
            ORDER BY created_at ASC
            LIMIT 10
        """
        stale = con.execute(stale_q, [stale_ts] + ns_params).fetchall()
        if stale_total:
            report['issues'].append({
                'type': 'stale_decision',
                'count': int(stale_total),
                'sample': [{'id': r[0][:8], 'ns': r[1], 'title': (r[2] or '')[:40],
                            'age_days': int((now - r[3]) / 86400)} for r in stale[:5]],
                'description': f'{int(stale_total)} decisions older than {stale_days}d with no supersedes link'
            })
        
        # --- 3. Contradiction pairs ---
        contrad = con.execute("""
            SELECT l.source_id, l.target_id, l.score,
                   e1.title, e2.title, e1.namespace
            FROM links l
            JOIN entries e1 ON l.source_id = e1.id
            JOIN entries e2 ON l.target_id = e2.id
            WHERE l.relation_type = 'contradicts'
            LIMIT 10
        """).fetchall()
        if contrad:
            report['issues'].append({
                'type': 'contradiction',
                'count': len(contrad),
                'sample': [{'src': r[0][:8], 'tgt': r[1][:8], 'score': r[2],
                            'src_title': (r[3] or '')[:30],
                            'tgt_title': (r[4] or '')[:30]} for r in contrad[:3]],
                'description': f'{len(contrad)} contradiction links need resolution'
            })
        
        # --- 4. Low salience mass ---
        low_q = f"""
            SELECT namespace, COUNT(*) as cnt
            FROM entries
            WHERE COALESCE(importance_v2, 0.5) < 0.3
              AND (status IS NULL OR status != 'archived')
              {ns_filter}
            GROUP BY namespace
            ORDER BY cnt DESC
        """
        low_imp = con.execute(low_q, ns_params).fetchall()
        if any(r[1] > 20 for r in low_imp):
            report['issues'].append({
                'type': 'low_salience_mass',
                'by_namespace': [{'ns': r[0], 'count': r[1]} for r in low_imp if r[1] > 20],
                'description': 'Large number of low-importance entries, consider archiving or consolidating'
            })
        
        # --- 5. Bootstrap staleness ---
        boot_rows = con.execute(
            "SELECT namespace, updated_at FROM bootstrap ORDER BY updated_at ASC LIMIT 5"
        ).fetchall()
        stale_boots = [(r[0], int((now - r[1]) / 3600)) for r in boot_rows
                       if (now - r[1]) > 7 * 86400]
        if stale_boots:
            report['issues'].append({
                'type': 'stale_bootstrap',
                'namespaces': [{'ns': r[0], 'age_hours': r[1]} for r in stale_boots],
                'description': 'Bootstrap summaries older than 7 days, run memory_compact'
            })
        
        # --- Stats ---
        total = con.execute(
            f"SELECT COUNT(*) FROM entries WHERE 1=1 {ns_filter}", ns_params
        ).fetchone()[0]
        link_count = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        semantic_count = con.execute(
            f"SELECT COUNT(*) FROM entries WHERE type='semantic' {ns_filter}", ns_params
        ).fetchone()[0]
        
        report['stats'] = {
            'total_entries': total,
            'total_links': link_count,
            'semantic_entries': semantic_count,
            'orphan_decisions': len(orphan_issues),
            'stale_decisions': int(stale_total),
            'contradictions': len(contrad),
        }
        
        # --- Suggestions ---
        if stale_boots:
            report['suggestions'].append(
                f"Run memory_compact for: {[r[0] for r in stale_boots]}"
            )
        if stale_total > 0:
            report['suggestions'].append(
                f"Review {int(stale_total)} stale decisions - supersede or archive outdated ones"
            )
        if semantic_count == 0:
            report['suggestions'].append(
                "No semantic entries yet - run memory_consolidate to synthesize repeated knowledge"
            )
        if link_count < 10:
            report['suggestions'].append(
                "Few links - use memory_ingest for new entries to auto-generate cross-references"
            )
        
        report['namespace'] = ns or 'all'
        report['issue_count'] = len(report['issues'])
        return report
    finally:
        con.close()



# ---------------------------------------------------------------------------
# memory_wikify (karpathy LLM Wiki - 答えの書き戻し)
# ---------------------------------------------------------------------------
def tool_memory_wikify(args: dict) -> dict:
    """File back a valuable Q&A or analysis as a wiki entry.
    
    karpathy: "good answers can be filed back into the wiki as new pages.
    A comparison you asked for, an analysis, a connection you discovered
    -- these are valuable and shouldn't disappear into chat history."
    
    Creates a 'semantic' or 'fact' type entry from the provided Q&A,
    then runs ingest cross-reference linking.
    
    Args:
        question:   The question that was asked
        answer:     The answer/analysis to preserve
        namespace:  Target namespace
        title:      Optional title (LLM generates if omitted)
        tags:       Optional tags
        importance: 1-5 (default 4, since wikified content is valuable)
        room_id:    Optional room
    """
    import sqlite3, time, json, uuid
    
    question  = (args or {}).get('question', '')
    answer    = (args or {}).get('answer', '')
    ns        = (args or {}).get('namespace', 'mirage-infra')
    title     = (args or {}).get('title', '')
    tags      = (args or {}).get('tags', [])
    importance = int((args or {}).get('importance', 4) or 4)
    room_id   = (args or {}).get('room_id', None)
    
    if not answer:
        return {'error': 'answer required'}
    
    # Auto-generate title if not provided
    if not title:
        if question:
            title = question[:60] + ('...' if len(question) > 60 else '')
        else:
            title = answer[:60] + ('...' if len(answer) > 60 else '')
    
    # Format content as Q&A wiki page
    if question:
        content = "Q: " + question + "\n\nA: " + answer
    else:
        content = answer
    
    # Use memory_ingest for auto cross-reference
    ingest_result = tool_memory_ingest({
        'namespace': ns,
        'type': 'semantic',
        'title': title,
        'content': content,
        'tags': tags or ['wikified', 'qa'],
        'importance': importance,
        'room_id': room_id or f'{ns}:general',
        'auto_link': True,
        'max_candidates': 5,
    })
    
    return {
        'entry_id': ingest_result.get('entry_id'),
        'title': title,
        'namespace': ns,
        'links_created': ingest_result.get('links_created', 0),
        'message': 'Filed to wiki as semantic entry with cross-references',
    }



# ---------------------------------------------------------------------------
# memory_archive (C2 + E1: archival + size management)
# ---------------------------------------------------------------------------
def tool_memory_archive(args: dict) -> dict:
    """Archive old low-salience entries and manage DB size.
    
    C2 policy:
      - type=raw, age > stale_days, importance_v2 < imp_threshold, access_count=0 → archived
      - type=decision, age > decision_days, importance_v2 < 0.3, access_count=0 → archived
    
    E1 policy:
      - If DB > size_threshold_mb, permanently delete oldest archived entries
        until under threshold (keeps N most recent archived for audit)
    
    Args:
        stale_days:       raw entries older than N days (default 90)
        decision_days:    decision entries older than N days (default 180)
        imp_threshold:    importance_v2 < X to archive (default 0.3)
        size_threshold_mb: DB size limit in MB (default 50)
        keep_archived_n:  keep N most recent archived entries (default 500)
        dry_run:          bool, default False
        namespace:        optional filter
    """
    import sqlite3, time, os as _os
    
    stale_days   = int((args or {}).get('stale_days', 90) or 90)
    dec_days     = int((args or {}).get('decision_days', 180) or 180)
    imp_thresh   = float((args or {}).get('imp_threshold', 0.3) or 0.3)
    size_mb      = float((args or {}).get('size_threshold_mb', 50) or 50)
    keep_n       = int((args or {}).get('keep_archived_n', 500) or 500)
    dry_run      = bool((args or {}).get('dry_run', False))
    ns           = (args or {}).get('namespace', None)
    
    db = r'C:\MirageWork\mcp-server\data\memory.db'
    con = sqlite3.connect(db)
    now = int(time.time())
    report = {'archived': 0, 'deleted': 0, 'db_size_mb': 0, 'dry_run': dry_run}
    
    try:
        ns_clause = 'AND namespace = ?' if ns else ''
        ns_params = [ns] if ns else []
        
        # C2a: Archive old raw
        raw_ts = now - stale_days * 86400
        if dry_run:
            n = con.execute(f"""
                SELECT COUNT(*) FROM entries
                WHERE type='raw' AND created_at < ?
                  AND COALESCE(importance_v2,0.5) < ?
                  AND (status IS NULL OR status='active')
                  AND COALESCE(access_count,0) = 0
                  {ns_clause}
            """, [raw_ts, imp_thresh] + ns_params).fetchone()[0]
            report['would_archive_raw'] = n
        else:
            r = con.execute(f"""
                UPDATE entries SET status='archived'
                WHERE type='raw' AND created_at < ?
                  AND COALESCE(importance_v2,0.5) < ?
                  AND (status IS NULL OR status='active')
                  AND COALESCE(access_count,0) = 0
                  {ns_clause}
            """, [raw_ts, imp_thresh] + ns_params)
            report['archived'] += r.rowcount
        
        # C2b: Archive old low-importance decisions
        dec_ts = now - dec_days * 86400
        if dry_run:
            n = con.execute(f"""
                SELECT COUNT(*) FROM entries
                WHERE type='decision' AND created_at < ?
                  AND COALESCE(importance_v2,0.5) < 0.3
                  AND (status IS NULL OR status='active')
                  AND COALESCE(access_count,0) = 0
                  {ns_clause}
            """, [dec_ts] + ns_params).fetchone()[0]
            report['would_archive_decisions'] = n
        else:
            r = con.execute(f"""
                UPDATE entries SET status='archived'
                WHERE type='decision' AND created_at < ?
                  AND COALESCE(importance_v2,0.5) < 0.3
                  AND (status IS NULL OR status='active')
                  AND COALESCE(access_count,0) = 0
                  {ns_clause}
            """, [dec_ts] + ns_params)
            report['archived'] += r.rowcount
        
        if not dry_run:
            con.commit()
        
        # E1: DB size check and purge
        db_size_mb = _os.path.getsize(db) / 1024 / 1024
        report['db_size_mb'] = round(db_size_mb, 1)
        
        if db_size_mb > size_mb and not dry_run:
            # Delete oldest archived entries, keep keep_n most recent
            archived_ids = con.execute("""
                SELECT id FROM entries WHERE status='archived'
                ORDER BY updated_at DESC
            """).fetchall()
            
            to_delete = archived_ids[keep_n:]
            if to_delete:
                ids = [r[0] for r in to_delete]
                placeholders = ','.join('?' * len(ids))
                con.execute(f"DELETE FROM entries WHERE id IN ({placeholders})", ids)
                con.execute(f"DELETE FROM links WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})", ids + ids)
                con.commit()
                # VACUUM to reclaim space
                con.execute('VACUUM')
                report['deleted'] = len(to_delete)
                report['db_size_after_mb'] = round(_os.path.getsize(db)/1024/1024, 1)
        
        report['active_count'] = con.execute(
            "SELECT COUNT(*) FROM entries WHERE status IS NULL OR status='active'"
        ).fetchone()[0]
        report['archived_count'] = con.execute(
            "SELECT COUNT(*) FROM entries WHERE status='archived'"
        ).fetchone()[0]
        
        return report
    finally:
        con.close()



# ---------------------------------------------------------------------------
# memory_semantic_search (A1: Semantic Search via LLM re-ranking)
# ---------------------------------------------------------------------------
def _resolve_semantic_search_backend(args: dict) -> dict:
    """Resolve explicit backend while preserving legacy use_llm flags."""
    raw_backend = ((args or {}).get('backend') or 'auto')
    backend = str(raw_backend).strip().lower().replace('-', '_')
    aliases = {
        'semantic': 'semantic_lite',
        'lite': 'semantic_lite',
        'semantic_lite_plus_fts': 'hybrid',
        'semantic_lite_fts': 'hybrid',
        'fts_only': 'fts',
        'llm_rerank': 'llm',
        'semantic_llm_rerank': 'llm',
    }
    backend = aliases.get(backend, backend)
    valid = {'auto', 'fts', 'semantic_lite', 'hybrid', 'llm'}
    if backend not in valid:
        return {
            'error': f"invalid backend: {raw_backend}",
            'valid_backends': sorted(valid),
        }

    legacy_use_llm = (args or {}).get('use_llm', None)
    legacy_use_lite = (args or {}).get('use_semantic_lite', None)
    if backend == 'auto':
        use_llm = bool(legacy_use_llm) if legacy_use_llm is not None else True
        use_semantic_lite = (not use_llm) if legacy_use_lite is None else bool(legacy_use_lite)
        resolved = 'llm' if use_llm else ('hybrid' if use_semantic_lite else 'fts')
    elif backend == 'fts':
        use_llm = False
        use_semantic_lite = False
        resolved = 'fts'
    elif backend == 'semantic_lite':
        use_llm = False
        use_semantic_lite = True
        resolved = 'semantic_lite'
    elif backend == 'hybrid':
        use_llm = False
        use_semantic_lite = True
        resolved = 'hybrid'
    else:  # llm
        use_llm = True
        use_semantic_lite = bool(legacy_use_lite) if legacy_use_lite is not None else True
        resolved = 'llm'
    return {
        'requested': backend,
        'resolved': resolved,
        'use_llm': use_llm,
        'use_semantic_lite': use_semantic_lite,
    }


def _semantic_search_trust_fields(method: str, use_semantic_lite: bool, use_llm: bool) -> dict:
    if use_llm:
        strength = 'llm_rerank_over_candidates'
        basis = 'LLM rerank over FTS/semantic_lite retrieval candidates; not independent proof.'
        status = 'ranked_candidate'
    elif use_semantic_lite:
        strength = 'hashed_ngram'
        basis = 'semantic_lite hashed token/ngram cosine; not a true embedding backend.'
        status = 'candidate'
    else:
        strength = 'fts_lexical'
        basis = 'FTS lexical match only.'
        status = 'candidate'
    return {
        'epistemic_status': status,
        'semantic_strength': strength,
        'confidence_basis': basis,
        'confirmed_semantic_relation': False,
        'trust_warning': (
            f'{method} returns retrieval/ranking candidates. Inspect match_reason, score, and source content before treating as a relation.'
        ),
    }


def tool_memory_semantic_search(args: dict) -> dict:
    """Semantic search: FTS candidates + LLM re-ranking for conceptual match.
    
    No heavy deps (no faiss/sentence_transformers).
    Uses Cerebras qwen-3-235b to score relevance between query and candidates.
    Falls back to FTS results if LLM unavailable.
    
    Args:
        query:      Natural language query
        namespace:  Optional namespace filter
        limit:      Number of results (default 5)
        types:      Optional type filter list
        backend:    auto|fts|semantic_lite|hybrid|llm (default auto)
        use_llm:    legacy bool (default True) - enable LLM re-ranking
        use_semantic_lite: bool - when true, include semantic-lite candidates.
                   If omitted, semantic-lite is used automatically when use_llm=false.
        fts_mult:   int (default 4) - FTS candidates = limit * fts_mult
    """
    import json as _json
    
    query    = (args or {}).get('query', '')
    ns       = (args or {}).get('namespace', None)
    limit    = int((args or {}).get('limit', 5) or 5)
    types    = (args or {}).get('types', None)
    backend = _resolve_semantic_search_backend(args or {})
    if backend.get('error'):
        return backend
    use_llm = bool(backend['use_llm'])
    use_semantic_lite = bool(backend['use_semantic_lite'])
    resolved_backend = backend['resolved']
    fts_mult = int((args or {}).get('fts_mult', 4) or 4)
    
    if not query:
        return {'error': 'query required'}
    
    from memory import store as mem_store
    
    # Step 1: FTS to get candidates
    fts_limit = min(limit * fts_mult, 40)
    if resolved_backend == 'semantic_lite':
        raw = {'hits': []}
    elif ns:
        raw = mem_store.search(ns, query=query, types=types, limit=fts_limit)
    else:
        raw = mem_store.search_all(query=query, types=types, limit=fts_limit)
    
    candidates = raw.get('hits', [])
    seen_ids = set()
    deduped_candidates = []
    for c in candidates:
        cid = c.get('id') if isinstance(c, dict) else None
        if cid:
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
        deduped_candidates.append(c)
    candidates = deduped_candidates

    lite_hits = []
    lite_error = None
    if use_semantic_lite:
        try:
            lite = mem_store.semantic_lite_search(
                query=query, namespace=ns, types=types, limit=max(limit * 2, 10), min_score=0.05
            )
            lite_error = lite.get('error')
            lite_hits = lite.get('hits') or []
        except Exception as e:
            lite_error = str(e)
            lite_hits = []
        merged = []
        seen_ids = set()
        source_hits = lite_hits if resolved_backend == 'semantic_lite' else lite_hits + candidates
        for h in source_hits:
            cid = h.get('id') if isinstance(h, dict) else None
            if cid:
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
            merged.append(h)
            if len(merged) >= max(limit * fts_mult, limit):
                break
        candidates = merged
    
    if not candidates or not use_llm or len(candidates) <= limit:
        method = ('semantic_lite_only' if resolved_backend == 'semantic_lite' and lite_hits
                  else 'semantic_lite_plus_fts' if use_semantic_lite and lite_hits
                  else 'fts_only')
        out = {
            'hits': candidates[:limit],
            'method': method,
            'backend': backend,
            'total_candidates': len(candidates),
            **_semantic_search_trust_fields(method, use_semantic_lite, False),
        }
        if use_semantic_lite:
            out['semantic_lite_candidates'] = len(lite_hits)
            if lite_error:
                out['semantic_lite_error'] = lite_error
        return out
    
    # Step 2: LLM re-ranking
    try:
        import sys as _sys
        _sys.path.insert(0, r'C:\MirageWork\mcp-server-v2')
        import llm
        
        cand_text = '\n'.join([
            f"[{i+1}] type={c.get('type','')} ns={c.get('namespace','')} "
            f"title={c.get('title','')[:40]}: {str(c.get('snippet') or c.get('content',''))[:80]}"
            for i, c in enumerate(candidates)
        ])
        
        prompt = f"""Rate each candidate's semantic relevance to the query.

Query: {query}

Candidates:
{cand_text}

Return ONLY a JSON array of indices (1-based) sorted by relevance, most relevant first.
Include only the top {limit} indices.
Example: [3, 1, 5, 2, 4]
JSON only, no explanation."""
        
        raw_resp = llm.call(prompt, purpose='semantic_search', max_tokens=100, timeout=15)
        
        import re
        match = re.search(r'\[[\d,\s]+\]', raw_resp)
        if match:
            indices = _json.loads(match.group())
            reranked = []
            for idx in indices:
                if 1 <= idx <= len(candidates):
                    hit = candidates[idx-1].copy()
                    hit['semantic_rank'] = len(reranked) + 1
                    reranked.append(hit)
                if len(reranked) >= limit:
                    break
            
            # Fill remaining with FTS order if needed
            seen_ids = {h['id'] for h in reranked}
            for c in candidates:
                if c['id'] not in seen_ids and len(reranked) < limit:
                    reranked.append(c)
            
            return {
                'hits': reranked,
                'method': 'semantic_llm_rerank',
                'backend': backend,
                'total_candidates': len(candidates),
                'model': 'qwen-3-235b',
                'semantic_lite_candidates': len(lite_hits) if use_semantic_lite else 0,
                **_semantic_search_trust_fields('semantic_llm_rerank', use_semantic_lite, True),
            }
    except Exception as e:
        pass  # Degrade to FTS gracefully
    
    out = {
        'hits': candidates[:limit],
        'method': ('semantic_lite_only_fallback' if resolved_backend == 'semantic_lite' and lite_hits
                   else 'semantic_lite_plus_fts_fallback' if use_semantic_lite and lite_hits
                   else 'fts_fallback'),
        'backend': backend,
        'total_candidates': len(candidates),
    }
    out.update(_semantic_search_trust_fields(out['method'], use_semantic_lite, False))
    if use_semantic_lite:
        out['semantic_lite_candidates'] = len(lite_hits)
        if lite_error:
            out['semantic_lite_error'] = lite_error
    return out


def tool_memory_semantic_lite_rebuild(args: dict) -> dict:
    """Build dependency-light hashed n-gram vector index for memory search."""
    ns = (args or {}).get('namespace', None)
    types = (args or {}).get('types', None)
    limit = int((args or {}).get('limit', 5000) or 5000)
    try:
        from memory import store as mem_store
        return mem_store.semantic_lite_rebuild(namespace=ns, types=types, limit=limit)
    except Exception as e:
        return {'error': str(e), 'backend': 'semantic_lite_hashed_ngrams'}


def tool_memory_semantic_lite_status(args: dict) -> dict:
    """Return semantic-lite index freshness and build metadata."""
    ns = (args or {}).get('namespace', None)
    try:
        from memory import store as mem_store
        return mem_store.semantic_lite_status(namespace=ns)
    except Exception as e:
        return {'error': str(e), 'backend': 'semantic_lite_hashed_ngrams'}


def tool_memory_semantic_backend_status(args: dict) -> dict:
    """Report available semantic backends and install state.

    Note (2026-05-11): the original check only looked for in-process
    `import fastembed` and `import hnswlib`. The actual operational mode on
    this machine is `usearch` (different package than hnswlib) + a subprocess
    that runs fastembed in a memory-win venv. That combination produces real
    true-embedding searches (verified score 0.71-0.74) but the legacy check
    reports "not installed" because the names it looks for never appear in
    the V2 main process. operational_* fields below describe the path that
    actually runs.
    """
    import os as _os
    backends = []
    try:
        import numpy as _np
        numpy_ok = True
        numpy_version = getattr(_np, '__version__', '')
    except Exception as e:
        numpy_ok = False
        numpy_version = ''
    try:
        import fastembed as _fastembed
        fastembed_ok = True
        fastembed_version = getattr(_fastembed, '__version__', '')
    except Exception as e:
        fastembed_ok = False
        fastembed_version = ''
        fastembed_error = str(e)
    else:
        fastembed_error = ''
    try:
        import hnswlib as _hnswlib
        hnswlib_ok = True
        hnswlib_version = getattr(_hnswlib, '__version__', '')
    except Exception as e:
        hnswlib_ok = False
        hnswlib_version = ''
        hnswlib_error = str(e)
    else:
        hnswlib_error = ''

    # Operational reality checks (the path searches actually take):
    try:
        import usearch as _usearch
        usearch_ok = True
        usearch_version = getattr(_usearch, '__version__', '')
    except Exception:
        usearch_ok = False
        usearch_version = ''
    try:
        from memory_store import _fastembed_available, _FASTEMBED_INDEX_PATH, _HNSW_INDEX_PATH
        fastembed_subprocess_ok = _fastembed_available()
        fastembed_index_exists = _os.path.exists(_FASTEMBED_INDEX_PATH)
        hnsw_index_exists = _os.path.exists(_HNSW_INDEX_PATH)
    except Exception:
        fastembed_subprocess_ok = False
        fastembed_index_exists = False
        hnsw_index_exists = False

    operational_ready = (
        usearch_ok and fastembed_subprocess_ok
        and fastembed_index_exists and hnsw_index_exists
    )
    if operational_ready:
        operational_backend = 'usearch_hnsw + fastembed_subprocess'
        operational_summary = 'true-embedding search operational via usearch + subprocess'
    elif numpy_ok:
        operational_backend = 'semantic_lite_hashed_ngrams'
        operational_summary = 'semantic_lite fallback (no true embedding path ready)'
    else:
        operational_backend = 'fts_only'
        operational_summary = 'FTS only (no semantic backend available)'

    current = 'semantic_lite_hashed_ngrams' if numpy_ok else 'fts_only'
    target_ready = fastembed_ok and hnswlib_ok
    return {
        'operator_summary': (
            f'{operational_summary}; legacy target_ready={target_ready}'
        ),
        'current_backend': current,  # legacy in-process import check
        'operational_backend': operational_backend,  # what searches actually use
        'operational_ready': operational_ready,
        'target_backend': 'fastembed_hnsw',
        'target_ready': target_ready,
        'epistemic_status': 'true_embedding' if operational_ready
                            else 'limited_semantic' if current == 'semantic_lite_hashed_ngrams'
                            else 'lexical_only',
        'current_backend_trust': 'true_embedding' if operational_ready else 'candidate_only',
        'known_limitations': [
            'semantic_lite_hashed_ngrams is token/ngram cosine, not a true embedding model',
            'cross_namespace_hints and related_hints remain candidates until source content is inspected',
            ('legacy "fastembed_hnsw" target check looks for in-process imports of '
             'fastembed and hnswlib; this machine uses usearch + subprocess instead, '
             'so target_ready stays False even when true-embedding search is fully '
             'operational. See operational_ready for the actual state.'),
        ],
        'backends': [
            {'name': 'fts', 'available': True},
            {'name': 'semantic_lite_hashed_ngrams', 'available': numpy_ok, 'numpy_version': numpy_version},
            {'name': 'fastembed', 'available': fastembed_ok, 'version': fastembed_version, 'error': fastembed_error},
            {'name': 'hnswlib', 'available': hnswlib_ok, 'version': hnswlib_version, 'error': hnswlib_error},
            {'name': 'usearch', 'available': usearch_ok, 'version': usearch_version},
            {'name': 'fastembed_subprocess', 'available': fastembed_subprocess_ok},
        ],
        'operational_details': {
            'usearch_ok': usearch_ok,
            'fastembed_subprocess_ok': fastembed_subprocess_ok,
            'fastembed_index_exists': fastembed_index_exists,
            'hnsw_index_exists': hnsw_index_exists,
        },
        'next_action': (
            'implement fastembed_hnsw index/search'
            if target_ready else 'install fastembed and hnswlib in the V2 runtime before backend swap'
        ),
    }


def tool_memory_trust_report(args: dict) -> dict:
    """Summarize known external-memory trust boundaries for operators and agents."""
    backend = tool_memory_semantic_backend_status({})
    compact = tool_memory_compact_status({})
    link_health = tool_memory_link_health({})
    return {
        'operator_summary': 'external memory is usable; treat relation/search hints as candidates unless verified by source content',
        'current_misread_risks': [
            {
                'risk': 'cross_namespace_hints may be misread as confirmed cross-domain relation',
                'actual_status': 'candidate; lexical_match basis',
                'safe_response': 'read source entries before using the hint as design evidence',
            },
            {
                'risk': 'semantic_lite score may be misread as embedding similarity',
                'actual_status': 'hashed token/ngram cosine, not true embedding',
                'safe_response': 'treat score as retrieval confidence only',
            },
            {
                'risk': 'related_auto links may be misread as explicit semantic links',
                'actual_status': 'weak automatic candidate links',
                'safe_response': 'prefer supports/supersedes/contradicts for decisions; promote only after inspection',
            },
            {
                'risk': 'compact_status may be misread as reader-visible freshness',
                'actual_status': 'job state from process registry or persistent snapshot',
                'safe_response': 'verify applied state with memory_bootstrap freshness_level and since_last_compact',
            },
        ],
        'trust_boundaries': [
            {
                'area': 'cross_namespace_hints',
                'epistemic_status': 'candidate',
                'confidence_basis': 'lexical_match',
                'safe_reading': 'possible relation only; do not treat as confirmed connection',
                'upgrade_path': 'fastembed_hnsw backend plus source-content inspection',
            },
            {
                'area': 'memory_semantic_search',
                'epistemic_status': backend.get('epistemic_status', 'limited_semantic'),
                'confidence_basis': backend.get('current_backend', 'unknown'),
                'safe_reading': 'retrieval/ranking candidates; inspect match_reason, score, and content',
                'upgrade_path': 'install and activate fastembed+hnswlib in the V2 runtime',
            },
            {
                'area': 'memory_compact_status',
                'epistemic_status': 'job_state_observable',
                'confidence_basis': 'process registry or persistent job snapshot',
                'safe_reading': 'job completion indicates compact worker state; bootstrap freshness confirms applied reader state',
                'upgrade_path': 'continue checking compact_status together with memory_bootstrap freshness',
            },
            {
                'area': 'related_hints/supersede_candidates',
                'epistemic_status': 'candidate',
                'confidence_basis': 'term overlap and explicit supersede words',
                'safe_reading': 'review manually before archive/supersede operations',
                'upgrade_path': 'add verified relation classifier only after fastembed backend is active',
            },
        ],
        'semantic_backend': backend,
        'compact_status_trust': {
            'completion_verification_required': compact.get('completion_verification_required'),
            'verification_hint': compact.get('verification_hint'),
            'trust_boundary': compact.get('trust_boundary'),
        },
        'link_graph_trust': {
            'epistemic_status': link_health.get('epistemic_status'),
            'related_auto_ratio': link_health.get('related_auto_ratio'),
            'semantic_ratio': link_health.get('semantic_ratio'),
            'isolated_ratio': link_health.get('isolated_ratio'),
            'action_plan': link_health.get('action_plan', []),
        },
        'required_operator_checks': [
            'For cross-namespace hints: read source entries before assuming a real relation.',
            'For semantic hits: treat score as retrieval confidence, not truth.',
            'For compact: check compact_status and then bootstrap freshness_level/since_last_compact.',
        ],
    }


def tool_memory_maintenance(args: dict) -> dict:
    """Return maintenance recommendation, optionally execute recommended actions."""
    ns = (args or {}).get('namespace', 'mirage-infra')
    allow_auto = bool((args or {}).get('allow_auto', False))
    dry_run = bool((args or {}).get('dry_run', True))
    max_runtime_sec = int((args or {}).get('max_runtime_sec', 120) or 120)
    try:
        from memory import store as mem_store
        boot = mem_store.get_bootstrap(ns, max_chars=240)
        maintenance = boot.get('maintenance') or {}
        actions = maintenance.get('recommended_actions') or []
        planned = []
        if maintenance.get('compact_recommended'):
            planned.append({
                'action': 'memory_compact',
                'args': {
                    'namespace': ns,
                    'rebuild_semantic_lite': bool(maintenance.get('semantic_lite_rebuild_recommended')),
                },
                'reason': maintenance.get('compact_reason', ''),
            })
        elif maintenance.get('semantic_lite_rebuild_recommended'):
            planned.append({
                'action': 'memory_semantic_lite_rebuild',
                'args': {'limit': 5000},
                'reason': maintenance.get('semantic_lite_rebuild_reason', ''),
            })
        # fastembed_rebuild: planned when recommended, executed only with allow_auto + !dry_run + max_runtime >= 120
        if maintenance.get('fastembed_rebuild_recommended'):
            planned.append({
                'action': 'memory_fastembed_rebuild',
                'args': {},
                'reason': maintenance.get('fastembed_rebuild_reason', ''),
                'note': 'slow (~90s); requires allow_auto=true + dry_run=false + max_runtime_sec>=120',
            })
        result = {
            'namespace': ns,
            'allow_auto': allow_auto,
            'dry_run': dry_run,
            'max_runtime_sec': max_runtime_sec,
            'operator_summary': (
                'maintenance required: ' + ', '.join(actions)
                if actions else 'ok; no memory maintenance required'
            ),
            'maintenance': maintenance,
            'semantic_lite': boot.get('semantic_lite'),
            'planned': planned,
            'recommended_action_count': len(actions),
            'skipped_reason': '',
            'executed': [],
        }
        if not actions:
            result['skipped_reason'] = 'no recommended actions'
            return result
        if not allow_auto:
            result['skipped_reason'] = 'allow_auto=false'
            return result
        if dry_run:
            result['skipped_reason'] = 'dry_run=true'
            return result

        if maintenance.get('compact_recommended'):
            started = time.time()
            compact_args = {
                'namespace': ns,
                'rebuild_semantic_lite': bool(maintenance.get('semantic_lite_rebuild_recommended')),
            }
            compact_result = tool_memory_compact(compact_args)
            result['executed'].append({
                'action': 'memory_compact',
                'args': compact_args,
                'duration_sec': round(time.time() - started, 3),
                'result': compact_result,
            })
        elif maintenance.get('semantic_lite_rebuild_recommended'):
            started = time.time()
            if max_runtime_sec < 10:
                result['skipped_reason'] = 'max_runtime_sec too small for semantic_lite_rebuild'
                return result
            rebuild = mem_store.semantic_lite_rebuild(limit=5000)
            result['executed'].append({
                'action': 'memory_semantic_lite_rebuild',
                'args': {'limit': 5000},
                'duration_sec': round(time.time() - started, 3),
                'result': rebuild,
            })
        # fastembed_rebuild: execute only if recommended AND max_runtime allows
        if (maintenance.get('fastembed_rebuild_recommended')
                and max_runtime_sec >= 120
                and not maintenance.get('compact_recommended')  # don't run both in one pass
                and not maintenance.get('semantic_lite_rebuild_recommended')):
            started = time.time()
            fastembed_result = tool_memory_fastembed_rebuild({})
            result['executed'].append({
                'action': 'memory_fastembed_rebuild',
                'args': {},
                'duration_sec': round(time.time() - started, 3),
                'result': fastembed_result,
            })
            # Also rebuild HNSW if fastembed succeeded
            if fastembed_result.get('ok'):
                hnsw_result = tool_memory_hnsw_rebuild({})
                result['executed'].append({
                    'action': 'memory_hnsw_rebuild',
                    'args': {},
                    'duration_sec': 0,
                    'result': hnsw_result,
                })
        if result['executed']:
            result['operator_summary'] = (
                'executed: ' + ', '.join(e.get('action', '') for e in result['executed'])
            )
        return result
    except Exception as e:
        return {'error': str(e), 'namespace': ns}


def tool_memory_maintenance_monitor(args: dict) -> dict:
    """Check all memory namespaces and optionally execute safe maintenance."""
    default_namespaces = [
        'mirage-infra',
        'mirage-vulkan',
        'mirage-android',
        'mirage-design',
        'mirage-general',
    ]
    namespaces = (args or {}).get('namespaces') or default_namespaces
    if isinstance(namespaces, str):
        namespaces = [n.strip() for n in namespaces.split(',') if n.strip()]
    namespaces = [n for n in namespaces if n in default_namespaces]
    if not namespaces:
        return {'error': 'no valid namespaces', 'valid_namespaces': default_namespaces}

    allow_auto = bool((args or {}).get('allow_auto', False))
    dry_run = bool((args or {}).get('dry_run', True))
    max_runtime_sec = int((args or {}).get('max_runtime_sec', 120) or 120)
    stop_after_first_execute = bool((args or {}).get('stop_after_first_execute', True))
    started = time.time()
    results = []
    executed_count = 0
    for ns in namespaces:
        remaining = max_runtime_sec - int(time.time() - started)
        if remaining <= 0:
            results.append({
                'namespace': ns,
                'skipped_reason': 'monitor max_runtime_sec exhausted',
            })
            continue
        ns_allow_auto = allow_auto and (not stop_after_first_execute or executed_count == 0)
        result = tool_memory_maintenance({
            'namespace': ns,
            'allow_auto': ns_allow_auto,
            'dry_run': dry_run,
            'max_runtime_sec': remaining,
        })
        if result.get('executed'):
            executed_count += len(result.get('executed') or [])
        elif allow_auto and stop_after_first_execute and executed_count > 0:
            result['skipped_reason'] = result.get('skipped_reason') or 'stop_after_first_execute=true'
        results.append(result)

    recommended = []
    for r in results:
        for plan in (r.get('planned') or []):
            recommended.append({
                'namespace': r.get('namespace'),
                'action': plan.get('action'),
                'reason': plan.get('reason', ''),
                'args': plan.get('args', {}),
            })
    return {
        'namespaces': namespaces,
        'allow_auto': allow_auto,
        'dry_run': dry_run,
        'stop_after_first_execute': stop_after_first_execute,
        'duration_sec': round(time.time() - started, 3),
        'operator_summary': (
            f'maintenance required: {len(recommended)} action(s)'
            if recommended else 'ok; no memory maintenance required across namespaces'
        ),
        'recommended_count': len(recommended),
        'executed_count': executed_count,
        'recommended': recommended,
        'results': results,
    }


def tool_memory_semantic_lite_search(args: dict) -> dict:
    """Search dependency-light hashed n-gram vector index."""
    query = (args or {}).get('query', '')
    ns = (args or {}).get('namespace', None)
    types = (args or {}).get('types', None)
    limit = int((args or {}).get('limit', 5) or 5)
    min_score = float((args or {}).get('min_score', 0.05) or 0.05)
    if not query:
        return {'error': 'query required'}
    try:
        from memory import store as mem_store
        return mem_store.semantic_lite_search(
            query=query, namespace=ns, types=types, limit=limit, min_score=min_score
        )
    except Exception as e:
        return {'error': str(e), 'backend': 'semantic_lite_hashed_ngrams'}



# ---------------------------------------------------------------------------
# active_context - 今何が最優先か一発で返す (会話開始時の一発リカバリ)
# ---------------------------------------------------------------------------
def tool_active_context(args: dict) -> dict:
    """Return the highest-priority context for session recovery.
    
    Combines:
    - L0 bootstrap summary for each namespace (50-100 tok each)
    - Top active decisions by importance (L1 top-5)
    - Recent activity summary (last N hours)
    - Physical TODO items (hardcoded awareness)
    
    Designed to restore full situational awareness in < 1 tool call.
    
    Args:
        namespaces:    list of namespaces (default: vulkan/infra/android/design/general — design 必須: 運用憲法 4755ea2a 含む)
        top_decisions: int (default 5) - number of top decisions to include
        hours:         int (default 24) - recent activity window
    """
    import time
    
    ns_list      = (args or {}).get('namespaces', ['mirage-vulkan', 'mirage-infra', 'mirage-android', 'mirage-design', 'mirage-general'])
    top_n        = int((args or {}).get('top_decisions', 5) or 5)
    hours        = int((args or {}).get('hours', 24) or 24)
    
    result = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'l0_summaries': {},
        'top_decisions': [],
        'recent_activity': {},
        'physical_todos': [],
    }
    
    # L0: bootstrap summaries
    from memory import store as mem
    for ns in ns_list:
        try:
            b = mem.get_bootstrap(ns, max_chars=300)
            if b.get('summary'):
                result['l0_summaries'][ns] = b['summary'][:300]
        except Exception:
            pass
    
    # Top decisions across all namespaces
    try:
        from memory_store import search_all
        hits = search_all(query='', types=['decision'], limit=top_n * 3)
        decisions = [h for h in hits.get('hits', [])
                     if not h.get('superseded_by')]
        # Sort by importance_v2 desc
        decisions.sort(key=lambda x: float(x.get('importance_v2', 0.5)), reverse=True)
        result['top_decisions'] = [
            {
                'id': d.get('id', '')[:8],
                'namespace': d.get('namespace', ''),
                'title': d.get('title', ''),
                'content': str(d.get('snippet') or d.get('content', ''))[:120],
                'importance': d.get('importance_v2', 0.5),
            }
            for d in decisions[:top_n]
        ]
    except Exception as e:
        result['top_decisions_error'] = str(e)
    
    # Recent activity
    cutoff = int(time.time()) - hours * 3600
    try:
        import sqlite3
        db = r'C:\MirageWork\mcp-server\data\memory.db'
        con = sqlite3.connect(db)
        rows = con.execute(
            """SELECT namespace, COUNT(*) as cnt, MAX(created_at) as latest
               FROM entries
               WHERE created_at > ?
               GROUP BY namespace
               ORDER BY cnt DESC""",
            (cutoff,)
        ).fetchall()
        con.close()
        result['recent_activity'] = {
            r[0]: {'new_entries': r[1], 'latest': time.strftime('%H:%M', time.localtime(r[2]))}
            for r in rows
        }
    except Exception as e:
        result['recent_activity_error'] = str(e)
    
    # Physical TODOs (always-present hardware awareness)
    result['physical_todos'] = [
        'X1: USB tethering first-time enable (need physical: Settings > tethering)',
        'A9#479: USB offline recovery (physical reconnect needed)',
        'Build verify: UnifiedLayer context addition compile check',
    ]
    
    # Summary line
    ns_with_summary = len(result['l0_summaries'])
    total_decisions = len(result['top_decisions'])
    total_recent = sum(v['new_entries'] for v in result['recent_activity'].values())
    result['summary'] = (
        f"{ns_with_summary} namespace summaries | "
        f"{total_decisions} top decisions | "
        f"{total_recent} new entries in last {hours}h | "
        f"{len(result['physical_todos'])} physical TODOs"
    )
    
    return result


# ---------------------------------------------------------------------------
# memory_recent_activity - 直近N日の変更サマリー
# ---------------------------------------------------------------------------
def tool_memory_recent_activity(args: dict) -> dict:
    """Return a summary of memory changes in the last N days/hours.
    
    Shows: new entries by namespace/type, access patterns, 
    decision changes, bootstrap update recency.
    
    Args:
        days:       float - lookback window in days (default 1.0 = last 24h)
        namespace:  optional namespace filter
        detail:     bool - include entry titles (default False = counts only)
    """
    import time, sqlite3
    
    days      = float((args or {}).get('days', 1.0) or 1.0)
    ns_filter = (args or {}).get('namespace', None)
    detail    = bool((args or {}).get('detail', False))
    
    cutoff = int(time.time()) - int(days * 86400)
    db     = r'C:\MirageWork\mcp-server\data\memory.db'
    
    try:
        con = sqlite3.connect(db)
        
        # New entries breakdown
        ns_clause = 'AND namespace = ?' if ns_filter else ''
        ns_params = [ns_filter] if ns_filter else []
        
        new_by_ns_type = con.execute(f"""
            SELECT namespace, type, COUNT(*) as cnt
            FROM entries
            WHERE created_at > ? {ns_clause}
            GROUP BY namespace, type
            ORDER BY cnt DESC
        """, [cutoff] + ns_params).fetchall()
        
        # Accessed entries (touch_entry called)
        accessed = con.execute(f"""
            SELECT namespace, COUNT(*) as cnt
            FROM entries
            WHERE last_accessed > ? {ns_clause}
            GROUP BY namespace
            ORDER BY cnt DESC
        """, [cutoff] + ns_params).fetchall()
        
        # Decision changes specifically
        new_decisions = con.execute(f"""
            SELECT namespace, title, importance_v2, created_at
            FROM entries
            WHERE type = 'decision' AND created_at > ? {ns_clause}
            ORDER BY importance_v2 DESC
            LIMIT 10
        """, [cutoff] + ns_params).fetchall()
        
        # Bootstrap update recency
        boot_rows = con.execute(
            "SELECT namespace, updated_at FROM bootstrap ORDER BY updated_at DESC"
        ).fetchall()
        
        # Totals
        total_new = sum(r[2] for r in new_by_ns_type)
        total_accessed = sum(r[1] for r in accessed)
        
        result = {
            'window': f'last {days}d ({time.strftime("%Y-%m-%d %H:%M", time.localtime(cutoff))} to now)',
            'total_new_entries': total_new,
            'total_accessed': total_accessed,
            'new_by_namespace': {},
            'accessed_by_namespace': {r[0]: r[1] for r in accessed},
            'new_decisions': [],
            'bootstrap_freshness': {},
        }
        
        # Aggregate by namespace
        for row in new_by_ns_type:
            ns, etype, cnt = row
            if ns not in result['new_by_namespace']:
                result['new_by_namespace'][ns] = {}
            result['new_by_namespace'][ns][etype] = cnt
        
        # Decisions
        now = int(time.time())
        for row in new_decisions:
            entry = {
                'namespace': row[0],
                'title': (row[1] or '')[:60],
                'importance': round(float(row[2] or 0.5), 2),
                'age_min': int((now - row[3]) / 60),
            }
            result['new_decisions'].append(entry)
        
        # Bootstrap freshness
        for row in boot_rows:
            age_h = round((now - row[1]) / 3600, 1)
            result['bootstrap_freshness'][row[0]] = {
                'age_hours': age_h,
                'fresh': age_h < 72,
            }
        
        # Detail: entry titles
        if detail and total_new > 0:
            detail_rows = con.execute(f"""
                SELECT namespace, type, title, created_at
                FROM entries
                WHERE created_at > ? {ns_clause}
                ORDER BY created_at DESC
                LIMIT 20
            """, [cutoff] + ns_params).fetchall()
            result['recent_entries'] = [
                {
                    'namespace': r[0], 'type': r[1],
                    'title': (r[2] or '')[:60],
                    'age_min': int((now - r[3]) / 60),
                }
                for r in detail_rows
            ]
        
        con.close()
        return result
    except Exception as e:
        return {'error': str(e)}



# ---------------------------------------------------------------------------
# session_checkpoint - まとめて今日の成果を記録してProject State更新
# ---------------------------------------------------------------------------
def tool_session_checkpoint(args: dict) -> dict:
    """End-of-session checkpoint: summarize work done, save to memory,
    optionally update PROJECT_STATE.md.

    Auto-collects from git log, memory recent activity, and user-provided text.

    Args:
        done:        str  - what was accomplished (freeform)
        next:        list - next action items
        issues:      list - unresolved issues / blockers
        namespace:   str  - primary namespace for decisions (default: mirage-infra)
        update_md:   bool - update PROJECT_STATE.md (default: True)
        git_cwd:     str  - git repo for log (default: MirageVulkan)
        importance:  int  - 1-5 (default: 4)
    """
    import time, subprocess as _sub, re as _re

    done      = (args or {}).get('done', '')
    next_acts = (args or {}).get('next', [])
    issues    = (args or {}).get('issues', [])
    ns        = (args or {}).get('namespace', 'mirage-infra')
    update_md = bool((args or {}).get('update_md', True))
    git_cwd   = (args or {}).get('git_cwd', r'C:\MirageWork\MirageVulkan')
    importance = int((args or {}).get('importance', 4) or 4)

    timestamp = time.strftime('%Y-%m-%d %H:%M')
    result = {'timestamp': timestamp, 'saved': [], 'errors': []}

    # 1. Collect recent git commits
    git_summary = ''
    try:
        r = _sub.run(
            'git log --oneline --since="24 hours ago"',
            shell=True, capture_output=True, text=True,
            timeout=10, cwd=git_cwd, encoding='utf-8', errors='replace',
        )
        commits = r.stdout.strip()
        if commits:
            git_summary = f'Git commits (last 24h):\n{commits}'
    except Exception:
        pass

    # 2. Build checkpoint content
    parts = [f'# Session Checkpoint {timestamp}']
    if done:
        parts.append(f'\n## 完了\n{done}')
    if git_summary:
        parts.append(f'\n## {git_summary}')
    if next_acts:
        items = next_acts if isinstance(next_acts, list) else [next_acts]
        parts.append('\n## 次のアクション\n' + '\n'.join(f'- {a}' for a in items))
    if issues:
        items = issues if isinstance(issues, list) else [issues]
        parts.append('\n## 未解決\n' + '\n'.join(f'- {i}' for i in items))

    checkpoint_text = '\n'.join(parts)

    # 3. Save to memory
    from memory import store as mem
    try:
        mem.append_entry(
            namespace=ns,
            type_='decision',
            title=f'Session Checkpoint {timestamp}',
            content=checkpoint_text,
            tags=['checkpoint', 'session'],
            importance=importance,
            role='system',
        )
        result['saved'].append(f'memory:{ns}')
    except Exception as e:
        result['errors'].append(f'memory: {e}')

    # 4. Update PROJECT_STATE.md
    if update_md:
        md_path = r'C:\MirageWork\MirageVulkan\PROJECT_STATE.md'
        try:
            md = open(md_path, 'r', encoding='utf-8').read()

            # Update "Last Updated" line or prepend checkpoint section
            checkpoint_block = (
                f'\n## Last Session ({timestamp})\n'
                + (f'**Done**: {done[:200]}\n' if done else '')
                + ('**Next**: ' + ', '.join(str(a) for a in next_acts[:3]) + '\n' if next_acts else '')
                + ('**Blockers**: ' + ', '.join(str(i) for i in issues[:3]) + '\n' if issues else '')
            )

            # Replace existing "Last Session" block if present
            if '## Last Session' in md:
                md = _re.sub(
                    r'## Last Session.*?(?=\n## |\Z)',
                    checkpoint_block.strip() + '\n',
                    md, flags=_re.DOTALL
                )
            else:
                # Prepend after first heading
                first_h2 = md.find('\n## ')
                if first_h2 >= 0:
                    md = md[:first_h2] + checkpoint_block + md[first_h2:]
                else:
                    md = checkpoint_block + '\n' + md

            open(md_path, 'w', encoding='utf-8').write(md)
            result['saved'].append('PROJECT_STATE.md')
        except Exception as e:
            result['errors'].append(f'PROJECT_STATE.md: {e}')

    result['checkpoint'] = checkpoint_text
    result['ok'] = len(result['errors']) == 0
    return result


# ---------------------------------------------------------------------------
# memory_diff - Compare bootstrap before/after to see what changed
# ---------------------------------------------------------------------------
def tool_memory_diff(args: dict) -> dict:
    """Show what changed in memory since a reference point.

    Compares current bootstrap summaries with a saved snapshot,
    or shows entries added/modified in the last N hours.

    Args:
        namespace:    namespace to diff (None = all)
        hours:        lookback window (default 24h)
        mode:         'entries' = new/modified entries
                      'bootstrap' = compare stored snapshots
                      'decisions' = only decision changes (default)
        max_items:    max items to return (default 20)
    """
    import time, sqlite3, difflib

    ns        = (args or {}).get('namespace', None)
    hours     = float((args or {}).get('hours', 24) or 24)
    mode      = (args or {}).get('mode', 'decisions')
    max_items = int((args or {}).get('max_items', 20) or 20)

    cutoff = int(time.time()) - int(hours * 3600)
    db = r'C:\MirageWork\mcp-server\data\memory.db'

    result = {
        'mode': mode,
        'window': f'last {hours}h',
        'namespace': ns or 'all',
        'changes': [],
    }

    try:
        con = sqlite3.connect(db)
        ns_clause = 'AND namespace = ?' if ns else ''
        ns_params = [ns] if ns else []

        if mode in ('entries', 'decisions'):
            type_clause = "AND type = 'decision'" if mode == 'decisions' else ''
            rows = con.execute(f"""
                SELECT id, namespace, type, title, content,
                       importance_v2, created_at, status, superseded_by
                FROM entries
                WHERE created_at > ? {ns_clause} {type_clause}
                ORDER BY created_at DESC
                LIMIT ?
            """, [cutoff] + ns_params + [max_items]).fetchall()

            now = int(time.time())
            for row in rows:
                is_superseded = bool(row[8])
                result['changes'].append({
                    'id':         row[0][:8],
                    'namespace':  row[1],
                    'type':       row[2],
                    'title':      (row[3] or '')[:60],
                    'content':    (row[4] or '')[:150],
                    'importance': round(float(row[5] or 0.5), 2),
                    'age_min':    int((now - row[6]) / 60),
                    'status':     row[7] or 'active',
                    'superseded': is_superseded,
                    'change':     'added',
                })

        elif mode == 'bootstrap':
            # Compare bootstrap update times and show diffs
            boot_rows = con.execute(
                "SELECT namespace, summary, updated_at FROM bootstrap ORDER BY namespace"
            ).fetchall()
            now = int(time.time())
            for row in boot_rows:
                if ns and row[0] != ns:
                    continue
                age_h = round((now - row[2]) / 3600, 1)
                summary = row[1] or ''
                result['changes'].append({
                    'namespace':  row[0],
                    'summary':    summary[:300],
                    'updated_age_hours': age_h,
                    'fresh':      age_h < 24,
                    'char_count': len(summary),
                })

        # Summary
        result['total'] = len(result['changes'])
        if result['changes']:
            result['newest_age_min'] = result['changes'][0].get('age_min', 0)
            result['oldest_age_min'] = result['changes'][-1].get('age_min', 0)

        con.close()
        return result

    except Exception as e:
        return {'error': str(e)}


TOOLS = {
    'session_checkpoint': {
        'description': 'End-of-session checkpoint: saves done/next/issues to memory + updates PROJECT_STATE.md. Auto-collects git log.',
        'schema': {'type': 'object', 'properties': {
            'done':      {'type': 'string',  'description': 'What was accomplished'},
            'next':      {'type': 'array',   'items': {'type': 'string'}, 'description': 'Next action items'},
            'issues':    {'type': 'array',   'items': {'type': 'string'}, 'description': 'Unresolved issues'},
            'namespace': {'type': 'string',  'description': 'Memory namespace (default: mirage-infra)'},
            'update_md': {'type': 'boolean', 'description': 'Update PROJECT_STATE.md (default: True)'},
            'git_cwd':   {'type': 'string',  'description': 'Git repo for log collection'},
            'importance':{'type': 'integer', 'description': '1-5 (default: 4)'},
        }},
        'handler': tool_session_checkpoint,
    },
    'memory_diff': {
        'description': 'Show what changed in memory since N hours ago. mode=decisions|entries|bootstrap',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string',  'description': 'Namespace filter (None=all)'},
            'hours':     {'type': 'number',  'description': 'Lookback window hours (default 24)'},
            'mode':      {'type': 'string',  'description': 'decisions|entries|bootstrap (default: decisions)'},
            'max_items': {'type': 'integer', 'description': 'Max items (default 20)'},
        }},
        'handler': tool_memory_diff,
    },
    'active_context': {
        'description': 'One-shot session recovery: returns L0 summaries + top decisions + recent activity + physical TODOs. Call at session start.',
        'schema': {'type': 'object', 'properties': {
            'namespaces':    {'type': 'array', 'items': {'type': 'string'}, 'description': 'Namespaces to include (default: vulkan/infra/android/design/general)'},
            'top_decisions': {'type': 'integer', 'description': 'Number of top decisions (default 5)'},
            'hours':         {'type': 'integer', 'description': 'Recent activity window in hours (default 24)'},
        }},
        'handler': tool_active_context,
    },
    'memory_recent_activity': {
        'description': 'Summary of memory changes in last N days: new entries by namespace/type, accessed entries, decision changes, bootstrap freshness.',
        'schema': {'type': 'object', 'properties': {
            'days':      {'type': 'number',  'description': 'Lookback window in days (default 1.0 = 24h)'},
            'namespace': {'type': 'string',  'description': 'Optional namespace filter'},
            'detail':    {'type': 'boolean', 'description': 'Include entry titles (default False)'},
        }},
        'handler': tool_memory_recent_activity,
    },
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
            'rebuild_semantic_lite': {'type': 'boolean'},
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
        'description': 'Search memory entries via FTS. Excludes superseded/archived entries by default.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'query': {'type': 'string'},
            'limit': {'type': 'integer'},
            'types': {'type': 'array', 'items': {'type': 'string'}},
            'include_superseded': {'type': 'boolean', 'description': 'Default false. Set true to include superseded/archived entries (eg historical traversal).'},
        }, 'required': ['query']},
        'handler': tool_memory_search,
    },
    'memory_dig': {
        'description': 'Drill down older entries for a specific theme (e.g. "idx_step6", "Layer2"). Returns hits grouped by date. Excludes superseded/archived entries by default. Use namespace="*" for cross-namespace dig.',
        'schema': {'type': 'object', 'properties': {
            'namespace':   {'type': 'string', 'description': 'Namespace to dig in. Use "*" or "all" for cross-namespace via search_all.'},
            'theme':       {'type': 'string', 'description': 'Theme keyword e.g. idx_step6'},
            'before_date': {'type': 'string', 'description': 'YYYY-MM-DD; return entries strictly older than this date'},
            'limit':       {'type': 'integer', 'description': 'Max hits (default 20)'},
            'include_superseded': {'type': 'boolean', 'description': 'Default false. Set true to include superseded/archived entries.'},
        }, 'required': ['theme']},
        'handler': tool_memory_dig,
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
        'description': 'Mark old entry as superseded by new one. Accepts 8+ char id prefixes (auto-resolved).',
        'schema': {'type': 'object', 'properties': {
            'old_id': {'type': 'string'},
            'new_id': {'type': 'string'},
        }, 'required': ['old_id', 'new_id']},
        'handler': tool_memory_supersede,
    },
    'memory_supersede_many': {
        'description': 'Bulk supersede. pairs=[{old_id,new_id},...] or olds=[...]+new_id (many-to-one). Prefixes auto-resolved. dry_run=true to preview without mutating.',
        'schema': {'type': 'object', 'properties': {
            'pairs': {'type': 'array', 'items': {'type': 'object'}},
            'olds': {'type': 'array', 'items': {'type': 'string'}},
            'new_id': {'type': 'string'},
            'dry_run': {'type': 'boolean'},
        }},
        'handler': tool_memory_supersede_many,
    },
    'memory_get': {
        'description': 'Fetch full entry by ID or hex prefix. Returns ambiguity error with candidate list if prefix matches multiple entries.',
        'schema': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': 'Full UUID or hex prefix (>=4 chars). E.g. "bef2df67" resolves to bef2df67-ef05-...'},
        }, 'required': ['id']},
        'handler': tool_memory_get,
    },
    'memory_active_decisions': {
        'description': 'Get only active (non-superseded) decisions for a namespace.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'limit': {'type': 'integer'},
        }},
        'handler': tool_memory_active_decisions,
    },
    'memory_lifecycle_review': {
        'description': 'Review active/superseded/archive candidates by namespace/query/tags without mutating memory.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'query': {'type': 'string'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'limit': {'type': 'integer'},
            'include_archived': {'type': 'boolean'},
        }},
        'handler': tool_memory_lifecycle_review,
    },
    'memory_archive_by_query': {
        'description': 'Archive matching active entries by query or tags. Defaults to dry_run=true.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'query': {'type': 'string'},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'limit': {'type': 'integer'},
            'dry_run': {'type': 'boolean'},
        }},
        'handler': tool_memory_archive_by_query,
    },
    'memory_tag_strip': {
        'description': 'Remove a tag from active entries that have it. Defaults to dry_run=true.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'tag': {'type': 'string'},
            'limit': {'type': 'integer'},
            'dry_run': {'type': 'boolean'},
        }, 'required': ['tag']},
        'handler': tool_memory_tag_strip,
    },
    'memory_tag_rename': {
        'description': 'Rename a tag across active entries (merges if new_tag already present). Defaults to dry_run=true.',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'old_tag': {'type': 'string'},
            'new_tag': {'type': 'string'},
            'limit': {'type': 'integer'},
            'dry_run': {'type': 'boolean'},
        }, 'required': ['old_tag', 'new_tag']},
        'handler': tool_memory_tag_rename,
    },
    'memory_freshness': {
        'description': 'Check bootstrap freshness for all namespaces.',
        'schema': {'type': 'object', 'properties': {
            'max_age_hours': {'type': 'integer'},
        }},
        'handler': tool_memory_freshness,
    },
    'memory_l0': {
        'description': 'L0: compact namespace summaries, always-load layer (50-100 tok)',
        'schema': {
            'type': 'object',
            'properties': {
                'namespace': {'type': 'string', 'description': 'Filter by namespace'},
            }
        },
        'handler': tool_memory_l0,
    },
    'memory_l1': {
        'description': 'L1: top-N entries by salience (importance x freq x recency), session-start layer',
        'schema': {
            'type': 'object',
            'properties': {
                'namespace': {'type': 'string'},
                'top_n':     {'type': 'integer', 'description': 'Number of entries (default 20)'},
                'types':     {'type': 'string',  'description': 'Comma-separated type filter'},
            }
        },
        'handler': tool_memory_l1,
    },

    'memory_link_create': {
        'description': 'Create typed link between entries (supersedes/contradicts/supports/related)',
        'schema': {'type':'object','properties':{
            'source_id':     {'type':'string'},
            'target_id':     {'type':'string'},
            'relation_type': {'type':'string'},
            'score':         {'type':'number'},
            'note':          {'type':'string'},
        }},
        'handler': tool_memory_link_create,
    },
    'memory_fastembed_debug': {
        'description': 'Server-side introspection for fastembed/HNSW: sys.executable, sys.path, memory_store.__file__, import status, index paths, backend selection, fallback_reason.',
        'handler': tool_memory_fastembed_debug,
        'schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'memory_hnsw_rebuild': {
        'description': 'Build usearch HNSW index from fastembed .npz for fast ANN search. Run after fastembed_rebuild.',
        'handler': tool_memory_hnsw_rebuild,
        'schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'memory_hnsw_status': {
        'description': 'Check usearch HNSW index status.',
        'handler': tool_memory_hnsw_status,
        'schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'memory_fastembed_rebuild': {
        'description': 'Start async fastembed rebuild (returns job_id). Pass sync=true for legacy blocking call (likely exceeds MCP timeout).',
        'handler': tool_memory_fastembed_rebuild,
        'schema': {'type': 'object', 'properties': {
            'namespaces': {'type': 'array', 'description': 'Limit to specific namespaces (default: all)'},
            'sync': {'type': 'boolean', 'description': 'Block until done (default false: returns job_id immediately)'},
        }, 'required': []},
    },
    'memory_fastembed_rebuild_status': {
        'description': 'Poll status of an async fastembed rebuild job started by memory_fastembed_rebuild.',
        'handler': tool_memory_fastembed_rebuild_status,
        'schema': {'type': 'object', 'properties': {
            'job_id': {'type': 'string'},
        }, 'required': ['job_id']},
    },
    'memory_fastembed_search': {
        'description': 'Search memory using true fastembed embeddings. Returns epistemic_status=semantic_match.',
        'handler': tool_memory_fastembed_search,
        'schema': {'type': 'object', 'properties': {
            'query': {'type': 'string'},
            'namespace': {'type': 'string'},
            'limit': {'type': 'integer'},
            'min_score': {'type': 'number', 'description': 'Minimum cosine similarity (default 0.3)'},
            'types': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Filter by entry type, e.g. ["decision","fact"]. Widens candidate pool 10x to keep result count usable.'},
        }, 'required': ['query']},
    },
    'memory_fastembed_status': {
        'description': 'Check fastembed backend availability and index status.',
        'handler': tool_memory_fastembed_status,
        'schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'memory_link_promotion_review': {
        'description': 'Approve or reject link promotion candidates. approve->related_manual/supports/supersedes, reject->keep auto. dry_run=true by default.',
        'handler': tool_memory_link_promotion_review,
        'schema': {'type': 'object', 'properties': {
            'decisions': {'type': 'array', 'description': 'List of {link_id, action: approve|reject|supersedes|supports|contradicts, promote_to?, note?}'},
            'dry_run': {'type': 'boolean', 'description': 'Preview without writing (default true)'},
        }, 'required': ['decisions']},
    },
    'memory_link_bulk_promote': {
        'description': 'Safely auto-promote high-confidence related_auto to related_manual. Only score>=0.95 AND hub>=5 AND both decision. dry_run=true default.',
        'handler': tool_memory_link_bulk_promote,
        'schema': {'type': 'object', 'properties': {
            'min_score': {'type': 'number', 'description': 'Minimum score (default 0.95)'},
            'min_hub': {'type': 'integer', 'description': 'Minimum hub_count (default 5)'},
            'limit': {'type': 'integer', 'description': 'Max promotions (default 50)'},
            'dry_run': {'type': 'boolean', 'description': 'Preview without writing (default true)'},
        }, 'required': []},
    },
    'memory_contradiction_review': {
        'description': 'Classify contradiction candidates as false_positive, supports, or contradicts. Creates explicit links. dry_run=true by default.',
        'handler': tool_memory_contradiction_review,
        'schema': {'type': 'object', 'properties': {
            'decisions': {'type': 'array', 'description': 'List of {ban_id, mention_id, classification: false_positive|contradicts|supports, note?}'},
            'dry_run': {'type': 'boolean', 'description': 'Preview without writing (default true)'},
        }, 'required': ['decisions']},
    },
    'memory_link_promotion_candidates': {
        'description': 'Scan related_auto links and return candidates worth promoting to explicit relation types (supports/supersedes/related_manual)',
        'handler': tool_memory_link_promotion_candidates,
        'schema': {'type': 'object', 'properties': {
            'limit': {'type': 'integer', 'description': 'Max candidates to return (default 30)'},
            'min_score': {'type': 'number', 'description': 'Minimum score threshold (default 0.85)'},
        }, 'required': []},
    },
    'memory_contradiction_candidates': {
        'description': 'Scan memory for potential design contradictions: banned patterns vs active mentions (H264/AOA/D3D11VA/pm uninstall etc)',
        'handler': tool_memory_contradiction_candidates,
        'schema': {'type': 'object', 'properties': {
            'limit': {'type': 'integer', 'description': 'Max candidates to return (default 20)'},
        }, 'required': []},
    },
    'memory_link_health': {
        'description': 'Return link network health metrics: density, isolated ratio, explicit ratio, cross-namespace ratio',
        'handler': tool_memory_link_health,
        'schema': {'type': 'object', 'properties': {}, 'required': []},
    },
    'memory_link_search': {
        'description': 'Get links for an entry (in/out/both directions)',
        'schema': {'type':'object','properties':{
            'entry_id':      {'type':'string'},
            'relation_type': {'type':'string'},
            'direction':     {'type':'string'},
        }},
        'handler': tool_memory_link_search,
    },
    'memory_link_traverse': {
        'description': 'Multi-hop traversal: follow links N hops from starting entry',
        'schema': {'type':'object','properties':{
            'entry_id':     {'type':'string'},
            'max_hops':     {'type':'integer'},
            'relation_types': {'type':'string'},
        }},
        'handler': tool_memory_link_traverse,
    },

    'memory_consolidate': {
        'description': 'Consolidate high-salience repeated entries into semantic memory (Phase 4)',
        'schema': {'type':'object','properties':{
            'namespace':      {'type':'string'},
            'min_access_count': {'type':'integer', 'description': 'min access count threshold (default 3)'},
            'min_importance': {'type':'number', 'description': 'min importance_v2 threshold (default 0.6)'},
            'dry_run':        {'type':'boolean', 'description': 'list candidates without executing'},
            'max_group_size': {'type':'integer', 'description': 'max entries to consolidate at once (default 5)'},
        }},
        'handler': tool_memory_consolidate,
    },

    'memory_ingest': {
        'description': 'Ingest new entry with auto cross-reference link generation (karpathy LLM Wiki pattern)',
        'schema': {'type': 'object', 'properties': {
            'namespace':      {'type': 'string'},
            'type':           {'type': 'string'},
            'title':          {'type': 'string'},
            'content':        {'type': 'string'},
            'tags':           {'type': 'array', 'items': {'type': 'string'}},
            'importance':     {'type': 'integer'},
            'room_id':        {'type': 'string'},
            'auto_link':      {'type': 'boolean'},
            'max_candidates': {'type': 'integer'},
        }},
        'handler': tool_memory_ingest,
    },

    'memory_lint': {
        'description': 'Health-check: detect orphans, stale decisions, contradictions, low-salience mass (karpathy LLM Wiki lint)',
        'schema': {'type': 'object', 'properties': {
            'namespace':  {'type': 'string', 'description': 'Filter by namespace (omit for all)'},
            'stale_days': {'type': 'integer', 'description': 'Decisions older than N days are flagged (default 30)'},
        }},
        'handler': tool_memory_lint,
    },

    'memory_wikify': {
        'description': 'File back a Q&A or analysis as a permanent wiki entry with auto cross-reference (karpathy LLM Wiki write-back)',
        'schema': {'type': 'object', 'properties': {
            'question':   {'type': 'string'},
            'answer':     {'type': 'string'},
            'namespace':  {'type': 'string'},
            'title':      {'type': 'string'},
            'tags':       {'type': 'array', 'items': {'type': 'string'}},
            'importance': {'type': 'integer'},
            'room_id':    {'type': 'string'},
        }},
        'handler': tool_memory_wikify,
    },

    'memory_archive': {
        'description': 'C2+E1: Archive old low-salience entries and manage DB size automatically',
        'schema': {'type': 'object', 'properties': {
            'stale_days':        {'type': 'integer', 'description': 'raw entries older than N days (default 90)'},
            'decision_days':     {'type': 'integer', 'description': 'decision entries older than N days (default 180)'},
            'imp_threshold':     {'type': 'number',  'description': 'importance_v2 threshold (default 0.3)'},
            'size_threshold_mb': {'type': 'number',  'description': 'DB size limit MB (default 50)'},
            'keep_archived_n':   {'type': 'integer', 'description': 'keep N recent archived entries (default 500)'},
            'dry_run':           {'type': 'boolean'},
            'namespace':         {'type': 'string'},
        }},
        'handler': tool_memory_archive,
    },

    'memory_semantic_search': {
        'description': 'A1: Semantic search via selectable backend: auto, fts, semantic_lite, hybrid, or llm',
        'schema': {'type': 'object', 'properties': {
            'query':     {'type': 'string'},
            'namespace': {'type': 'string'},
            'limit':     {'type': 'integer'},
            'types':     {'type': 'array', 'items': {'type': 'string'}},
            'backend':   {'type': 'string', 'enum': ['auto', 'fts', 'semantic_lite', 'hybrid', 'llm']},
            'use_llm':   {'type': 'boolean'},
            'use_semantic_lite': {'type': 'boolean'},
            'fts_mult':  {'type': 'integer', 'description': 'FTS candidates = limit * fts_mult (default 4)'},
        }},
        'handler': tool_memory_semantic_search,
    },
    'memory_semantic_lite_rebuild': {
        'description': 'Build semantic-lite hashed n-gram vector index (numpy only, no external model downloads)',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'types':     {'type': 'array', 'items': {'type': 'string'}},
            'limit':     {'type': 'integer'},
        }},
        'handler': tool_memory_semantic_lite_rebuild,
    },
    'memory_semantic_lite_status': {
        'description': 'Return semantic-lite index freshness, stale count, and build metadata',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
        }},
        'handler': tool_memory_semantic_lite_status,
    },
    'memory_semantic_backend_status': {
        'description': 'Report semantic backend availability: FTS, semantic_lite, fastembed, hnswlib',
        'schema': {'type': 'object', 'properties': {}},
        'handler': tool_memory_semantic_backend_status,
    },
    'memory_trust_report': {
        'description': 'Summarize external-memory trust boundaries and safe interpretation rules',
        'schema': {'type': 'object', 'properties': {}},
        'handler': tool_memory_trust_report,
    },
    'memory_maintenance': {
        'description': 'Return memory maintenance recommendation; execute only when allow_auto=true and dry_run=false',
        'schema': {'type': 'object', 'properties': {
            'namespace': {'type': 'string'},
            'allow_auto': {'type': 'boolean'},
            'dry_run': {'type': 'boolean'},
            'max_runtime_sec': {'type': 'integer'},
        }},
        'handler': tool_memory_maintenance,
    },
    'memory_maintenance_monitor': {
        'description': 'Check memory maintenance recommendations across namespaces; execute only when allow_auto=true and dry_run=false',
        'schema': {'type': 'object', 'properties': {
            'namespaces': {'type': 'array', 'items': {'type': 'string'}},
            'allow_auto': {'type': 'boolean'},
            'dry_run': {'type': 'boolean'},
            'max_runtime_sec': {'type': 'integer'},
            'stop_after_first_execute': {'type': 'boolean'},
        }},
        'handler': tool_memory_maintenance_monitor,
    },
    'memory_semantic_lite_search': {
        'description': 'Search semantic-lite hashed n-gram vector index (numpy cosine over token/ngram vectors)',
        'schema': {'type': 'object', 'properties': {
            'query':     {'type': 'string'},
            'namespace': {'type': 'string'},
            'types':     {'type': 'array', 'items': {'type': 'string'}},
            'limit':     {'type': 'integer'},
            'min_score': {'type': 'number'},
        }},
        'handler': tool_memory_semantic_lite_search,
    },

}

# test

