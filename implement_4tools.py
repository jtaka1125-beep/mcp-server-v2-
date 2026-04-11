#!/usr/bin/env python3
"""
implement_4tools.py - 4つの新ツールを実装する
1. git_diff             → tools/system.py に追加
2. fail_log in task     → tools/task.py の _run_claude_async 改修
3. active_context       → tools/memory.py に追加
4. memory_recent_activity → tools/memory.py に追加
"""

import re
import os

BASE = r'C:\MirageWork\mcp-server-v2'


# ============================================================
# 1. git_diff → tools/system.py
# ============================================================
def patch_system_py():
    path = os.path.join(BASE, 'tools', 'system.py')
    content = open(path, 'r', encoding='utf-8').read()

    git_diff_func = '''
# ---------------------------------------------------------------------------
# git_diff - Show uncommitted changes (truncates large files)
# ---------------------------------------------------------------------------
def tool_git_diff(args: dict) -> dict:
    """Show git diff with smart truncation per-file.
    
    Args:
        cwd:        working directory (default MIRAGE_DIR)
        staged:     bool - show staged diff (default False = working tree)
        stat_only:  bool - show only --stat (default False)
        max_lines:  int  - max diff lines total (default 200)
        path:       str  - limit diff to specific file/dir
    """
    cwd       = (args or {}).get('cwd', MIRAGE_DIR)
    staged    = bool((args or {}).get('staged', False))
    stat_only = bool((args or {}).get('stat_only', False))
    max_lines = int((args or {}).get('max_lines', 200) or 200)
    path      = (args or {}).get('path', '')

    try:
        # Always get stat first
        stat_cmd = ['git', 'diff', '--stat']
        if staged:
            stat_cmd.insert(2, '--staged')
        if path:
            stat_cmd.append('--')
            stat_cmd.append(path)
        stat_r = subprocess.run(
            stat_cmd, capture_output=True, text=True,
            timeout=10, cwd=cwd, encoding='utf-8', errors='replace',
        )
        stat_out = stat_r.stdout.strip()

        if stat_only or not stat_out:
            return {
                'stat': stat_out or '(no changes)',
                'diff': None,
                'truncated': False,
            }

        # Get full diff
        diff_cmd = ['git', 'diff', '--no-color']
        if staged:
            diff_cmd.insert(2, '--staged')
        if path:
            diff_cmd += ['--', path]
        diff_r = subprocess.run(
            diff_cmd, capture_output=True, text=True,
            timeout=15, cwd=cwd, encoding='utf-8', errors='replace',
        )
        lines = diff_r.stdout.split('\\n')
        truncated = len(lines) > max_lines
        if truncated:
            # Keep first max_lines, add summary
            shown = lines[:max_lines]
            omitted = len(lines) - max_lines
            shown.append(f'\\n... [{omitted} more lines omitted - use stat_only=true or path= to narrow]')
            diff_out = '\\n'.join(shown)
        else:
            diff_out = diff_r.stdout

        return {
            'stat': stat_out,
            'diff': diff_out,
            'truncated': truncated,
            'total_lines': len(lines),
        }
    except Exception as e:
        return {'error': str(e)}

'''

    # Insert before TOOLS = {
    if 'def tool_git_diff(' not in content:
        content = content.replace(
            '\n# ---------------------------------------------------------------------------\n# ツール登録テーブル',
            git_diff_func + '\n# ---------------------------------------------------------------------------\n# ツール登録テーブル'
        )

    # Add to TOOLS dict - after git_status entry
    git_diff_entry = """    'git_diff': {
        'description': 'Show uncommitted changes with smart per-file truncation. Use stat_only=true for overview, path= to narrow.',
        'schema': {'type': 'object', 'properties': {
            'cwd':       {'type': 'string'},
            'staged':    {'type': 'boolean', 'description': 'Show staged diff'},
            'stat_only': {'type': 'boolean', 'description': 'Show only --stat overview'},
            'max_lines': {'type': 'integer', 'description': 'Max diff lines (default 200)'},
            'path':      {'type': 'string',  'description': 'Limit to file or directory'},
        }},
        'handler': tool_git_diff,
    },
"""

    if "'git_diff'" not in content:
        content = content.replace(
            "    'git_status': {",
            git_diff_entry + "    'git_status': {"
        )

    open(path, 'w', encoding='utf-8').write(content)
    print('✓ git_diff added to system.py')


# ============================================================
# 2. Auto-log on failure → tools/task.py
# ============================================================
def patch_task_py():
    path = os.path.join(BASE, 'tools', 'task.py')
    content = open(path, 'r', encoding='utf-8').read()

    # Replace the failure output section to include log tail
    old_error = (
        "    except subprocess.TimeoutExpired:\n"
        "        with _tasks_lock:\n"
        "            _tasks[task_id]['status'] = 'timeout'\n"
        "            _tasks[task_id]['output'] = 'ERROR: timeout after 300s'\n"
        "    except Exception as e:\n"
        "        with _tasks_lock:\n"
        "            _tasks[task_id]['status'] = 'error'\n"
        "            _tasks[task_id]['error'] = str(e)\n"
    )

    new_error = (
        "    except subprocess.TimeoutExpired:\n"
        "        log_tail = _get_server_log_tail(30)\n"
        "        with _tasks_lock:\n"
        "            _tasks[task_id]['status'] = 'timeout'\n"
        "            _tasks[task_id]['output'] = (\n"
        "                'ERROR: timeout after 300s'\n"
        "                + (f'\\n\\n--- Server Log (last 30 lines) ---\\n{log_tail}' if log_tail else '')\n"
        "            )\n"
        "    except Exception as e:\n"
        "        log_tail = _get_server_log_tail(20)\n"
        "        with _tasks_lock:\n"
        "            _tasks[task_id]['status'] = 'error'\n"
        "            _tasks[task_id]['error'] = str(e)\n"
        "            _tasks[task_id]['output'] = (\n"
        "                f'ERROR: {e}'\n"
        "                + (f'\\n\\n--- Server Log (last 20 lines) ---\\n{log_tail}' if log_tail else '')\n"
        "            )\n"
    )

    # Also add the non-zero exit code case
    old_status_done = (
        "        with _tasks_lock:\n"
        "            _tasks[task_id]['status'] = 'completed'\n"
        "            _tasks[task_id]['output'] = output\n"
    )
    new_status_done = (
        "        # Attach server log tail on non-zero exit\n"
        "        if result.returncode != 0:\n"
        "            log_tail = _get_server_log_tail(20)\n"
        "            if log_tail:\n"
        "                output += f'\\n\\n--- Server Log (last 20 lines) ---\\n{log_tail}'\n"
        "        with _tasks_lock:\n"
        "            _tasks[task_id]['status'] = 'completed'\n"
        "            _tasks[task_id]['output'] = output\n"
    )

    # Add helper function before _run_claude_async
    helper = (
        "# ---------------------------------------------------------------------------\n"
        "# Server log tail helper (auto-attach on failure)\n"
        "# ---------------------------------------------------------------------------\n"
        "def _get_server_log_tail(n: int = 20) -> str:\n"
        "    \"\"\"Return last N lines of server.log, empty string on error.\"\"\"\n"
        "    import os as _os\n"
        "    log_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), 'logs', 'server.log')\n"
        "    try:\n"
        "        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:\n"
        "            lines = f.readlines()\n"
        "        return ''.join(lines[-n:]).strip()\n"
        "    except Exception:\n"
        "        return ''\n"
        "\n"
    )

    if '_get_server_log_tail' not in content:
        content = content.replace(
            '# ---------------------------------------------------------------------------\n'
            '# Claude Code CLI 実行',
            helper +
            '# ---------------------------------------------------------------------------\n'
            '# Claude Code CLI 実行'
        )

    if old_error in content:
        content = content.replace(old_error, new_error)
        print('✓ error handler patched')
    else:
        print('⚠ error handler pattern not found (may already patched)')

    if old_status_done in content:
        content = content.replace(old_status_done, new_status_done)
        print('✓ exit code handler patched')
    else:
        print('⚠ exit code pattern not found')

    open(path, 'w', encoding='utf-8').write(content)
    print('✓ task.py patched')


# ============================================================
# 3 & 4. active_context + memory_recent_activity → tools/memory.py
# ============================================================
def patch_memory_py():
    path = os.path.join(BASE, 'tools', 'memory.py')
    content = open(path, 'r', encoding='utf-8').read()

    new_tools = '''

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
        namespaces:    list of namespaces (default: mirage-vulkan, mirage-infra, mirage-android)
        top_decisions: int (default 5) - number of top decisions to include
        hours:         int (default 24) - recent activity window
    """
    import time
    
    ns_list      = (args or {}).get('namespaces', ['mirage-vulkan', 'mirage-infra', 'mirage-android'])
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
        db = r'C:\\MirageWork\\mcp-server\\data\\memory.db'
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
    db     = r'C:\\MirageWork\\mcp-server\\data\\memory.db'
    
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

'''

    # Insert before TOOLS =
    tools_marker = '\nTOOLS = {'
    if 'def tool_active_context(' not in content:
        content = content.replace(tools_marker, new_tools + tools_marker)
        print('✓ active_context + memory_recent_activity functions added')

    # Add to TOOLS dict
    active_ctx_entry = """    'active_context': {
        'description': 'One-shot session recovery: returns L0 summaries + top decisions + recent activity + physical TODOs. Call at session start.',
        'schema': {'type': 'object', 'properties': {
            'namespaces':    {'type': 'array', 'items': {'type': 'string'}, 'description': 'Namespaces to include (default: vulkan/infra/android)'},
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
"""

    if "'active_context'" not in content:
        content = content.replace(
            "    'memory_bootstrap': {",
            active_ctx_entry + "    'memory_bootstrap': {"
        )
        print('✓ active_context + memory_recent_activity added to TOOLS')

    open(path, 'w', encoding='utf-8').write(content)
    print('✓ memory.py patched')


# ============================================================
# Run all patches
# ============================================================
if __name__ == '__main__':
    patch_system_py()
    patch_task_py()
    patch_memory_py()
    print('\nAll done!')
