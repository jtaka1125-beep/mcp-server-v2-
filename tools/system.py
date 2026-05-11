"""
tools/system.py - システム・ファイル系ツール
=============================================
シンプルなOS操作。subprocess/osのみ使用。
"""
import os
import sys
import subprocess
import json
import base64
import glob
import time
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import MIRAGE_DIR

log = logging.getLogger(__name__)

def _validate_path(path: str) -> tuple:
    """Reject obvious path-traversal patterns. Returns (ok, error_msg).

    Default behavior: deny `..` segments in the INPUT path (before
    normalization), because os.path.normpath silently resolves '..' which
    masked the traversal. If env var MIRAGE_PATH_ALLOWLIST is set
    (semicolon-separated list of allowed root dirs), also enforce that the
    resolved path is under one of those roots.
    """
    if not path:
        return (False, 'path required')
    # Check raw input for '..' segments BEFORE normpath collapses them.
    raw_parts = path.replace('\\', '/').split('/')
    if '..' in raw_parts:
        return (False, f'path traversal denied (.. segment): {path!r}')
    try:
        abs_path = os.path.abspath(path)
    except Exception as e:
        return (False, f'invalid path: {e}')
    allowlist = os.environ.get('MIRAGE_PATH_ALLOWLIST', '').strip()
    if allowlist:
        roots = [os.path.abspath(r.strip()) for r in allowlist.split(';') if r.strip()]
        if not any(abs_path == r or abs_path.startswith(r + os.sep) for r in roots):
            return (False, f'path {abs_path!r} not under any MIRAGE_PATH_ALLOWLIST root')
    return (True, '')


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------
def tool_run_command(args: dict) -> dict:
    # Kill switch: set MIRAGE_DISABLE_RUN_COMMAND=1 in env to refuse all run_command
    # calls. Useful when V2 is exposed via tunnel and you don't want arbitrary
    # shell execution available to that surface. Default: enabled (backwards compat).
    if os.environ.get('MIRAGE_DISABLE_RUN_COMMAND') == '1':
        return {'ok': False, 'exit_code': -1,
                'error': 'run_command disabled by MIRAGE_DISABLE_RUN_COMMAND=1'}

    cmd     = (args or {}).get('command', '')
    cwd     = (args or {}).get('cwd', MIRAGE_DIR)
    timeout = int((args or {}).get('timeout', 30) or 30)

    if not cmd:
        return {'error': 'command required'}
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd, encoding='utf-8', errors='replace',
        )
        return {
            'ok':       result.returncode == 0,
            'exit_code': result.returncode,
            'stdout':   result.stdout[-4000:] if result.stdout else '',
            'stderr':   result.stderr[-2000:] if result.stderr else '',
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'exit_code': -1, 'error': f'timeout after {timeout}s'}
    except Exception as e:
        return {'ok': False, 'exit_code': -1, 'error': str(e)}

# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
def tool_read_file(args: dict) -> dict:
    path = (args or {}).get('path', '')
    ok, msg = _validate_path(path)
    if not ok:
        return {'error': msg}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return {'content': content, 'size': len(content), 'path': path}
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
def tool_write_file(args: dict) -> dict:
    path    = (args or {}).get('path', '')
    content = (args or {}).get('content', '')
    ok, msg = _validate_path(path)
    if not ok:
        return {'error': msg}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return {'ok': True, 'path': path, 'size': len(content)}
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# write_file_b64
# ---------------------------------------------------------------------------
def tool_write_file_b64(args: dict) -> dict:
    path    = (args or {}).get('path', '')
    data_b64 = (args or {}).get('data_b64', '')
    mode    = (args or {}).get('mode', 'overwrite')  # 'overwrite' | 'append'
    ok, msg = _validate_path(path)
    if not ok:
        return {'error': msg}
    if not data_b64:
        return {'error': 'data_b64 required'}
    try:
        data = base64.b64decode(data_b64)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        write_mode = 'ab' if mode == 'append' else 'wb'
        with open(path, write_mode) as f:
            f.write(data)
        return {'ok': True, 'path': path, 'bytes': len(data), 'mode': mode}
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------
def tool_list_files(args: dict) -> dict:
    path    = (args or {}).get('path', MIRAGE_DIR)
    pattern = (args or {}).get('pattern', '*')
    # Validate path against traversal; pattern may legitimately include '*' etc.,
    # but not '..' segments.
    ok, msg = _validate_path(path)
    if not ok:
        return {'error': msg}
    if '..' in pattern.replace('\\', '/').split('/'):
        return {'error': f'pattern traversal denied: {pattern!r}'}
    try:
        full_pattern = os.path.join(path, pattern)
        files = glob.glob(full_pattern, recursive=False)
        result = []
        for f in sorted(files)[:200]:
            try:
                stat = os.stat(f)
                result.append({
                    'name': os.path.basename(f),
                    'path': f,
                    'size': stat.st_size,
                    'is_dir': os.path.isdir(f),
                })
            except Exception:
                pass
        return {'files': result, 'count': len(result), 'path': path}
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------
def tool_git_status(args: dict) -> dict:
    cwd = (args or {}).get('cwd', MIRAGE_DIR)
    try:
        r = subprocess.run(
            'git log --oneline -5 && git status --short',
            shell=True, capture_output=True, text=True,
            timeout=10, cwd=cwd, encoding='utf-8', errors='replace',
        )
        return {'output': r.stdout, 'ok': r.returncode == 0}
    except Exception as e:
        return {'error': str(e)}


def _load_git_dirty_policy(repo: str) -> dict:
    path = os.path.join(repo, 'health_dirty_policy.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        known = data.get('known_dirty_paths') or []
        return {
            'path': path,
            'known_dirty_paths': {str(p).replace('\\', '/') for p in known},
        }
    except Exception:
        return {'path': path, 'known_dirty_paths': set()}


def _classify_git_status_line(line: str, known_dirty_paths=None) -> dict:
    known_dirty_paths = known_dirty_paths or set()
    status = line[:2]
    path = line[3:].strip() if len(line) > 3 else ''
    normalized = path.replace('\\', '/')
    category = 'source_changes'
    if normalized in known_dirty_paths:
        category = 'known_dirty'
    elif '__pycache__/' in normalized or normalized.endswith('.pyc'):
        category = 'generated_pycache'
    elif normalized.startswith('.pytest_cache/') or '/.pytest_cache/' in normalized:
        category = 'generated_cache'
    elif status == '??':
        lower = os.path.basename(normalized).lower()
        if lower.startswith('_') or lower.startswith('scratch') or lower.endswith('_smoke.py'):
            category = 'untracked_scratch'
        else:
            category = 'untracked_files'
    elif normalized.endswith(('.log', '.pid', '.heartbeat')):
        category = 'runtime_artifacts'
    return {'status': status, 'path': path, 'category': category}


def tool_git_dirty_report(args: dict) -> dict:
    """Classify git dirty files so health checks can separate source changes from generated noise."""
    repos = (args or {}).get('repos') or [
        r'C:\MirageWork\mcp-server-v2',
        r'C:\MirageWork\mcp-server',
        r'C:\MirageWork\mirage-shared',
    ]
    if isinstance(repos, str):
        repos = [p.strip() for p in repos.split(',') if p.strip()]
    reports = []
    for repo in repos:
        repo = os.path.abspath(repo)
        item = {
            'repo': repo,
            'ok': False,
            'dirty': False,
            'policy_path': os.path.join(repo, 'health_dirty_policy.json'),
            'warnings': [],
            'counts': {},
            'items': [],
        }
        try:
            r = subprocess.run(
                ['git', 'status', '--short'],
                capture_output=True, text=True, timeout=10, cwd=repo,
                encoding='utf-8', errors='replace',
            )
            item['ok'] = r.returncode == 0
            stderr_lines = [ln.strip() for ln in (r.stderr or '').splitlines() if ln.strip()]
            item['warnings'] = stderr_lines[:20]
            if r.returncode != 0:
                item['error'] = (r.stderr or r.stdout)[-1000:]
            else:
                policy = _load_git_dirty_policy(repo)
                item['policy_path'] = policy['path']
                lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
                classified = [
                    _classify_git_status_line(ln, policy['known_dirty_paths'])
                    for ln in lines
                ]
                counts = {}
                for c in classified:
                    counts[c['category']] = counts.get(c['category'], 0) + 1
                item['dirty'] = bool(classified)
                item['counts'] = counts
                item['items'] = classified[:200]
        except Exception as e:
            item['error'] = str(e)
        reports.append(item)
    total_dirty = sum(sum(r.get('counts', {}).values()) for r in reports)
    source_dirty = sum(r.get('counts', {}).get('source_changes', 0) for r in reports)
    known_dirty = sum(r.get('counts', {}).get('known_dirty', 0) for r in reports)
    generated_dirty = sum(
        r.get('counts', {}).get('generated_pycache', 0)
        + r.get('counts', {}).get('generated_cache', 0)
        + r.get('counts', {}).get('runtime_artifacts', 0)
        for r in reports
    )
    scratch_dirty = sum(r.get('counts', {}).get('untracked_scratch', 0) for r in reports)
    untracked_dirty = sum(r.get('counts', {}).get('untracked_files', 0) for r in reports)
    git_warnings = sum(len(r.get('warnings') or []) for r in reports)
    return {
        'ok': all(r.get('ok') for r in reports),
        'dirty': total_dirty > 0,
        'total_dirty': total_dirty,
        'source_dirty': source_dirty,
        'known_dirty': known_dirty,
        'generated_dirty': generated_dirty,
        'scratch_dirty': scratch_dirty,
        'untracked_dirty': untracked_dirty,
        'git_warnings': git_warnings,
        'repos': reports,
    }


def _http_json(url: str, timeout: float = 3.0) -> dict:
    import urllib.request
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode('utf-8', errors='replace')
            return {
                'ok': 200 <= int(r.status) < 300,
                'status': int(r.status),
                'latency_ms': round((time.time() - started) * 1000, 3),
                'json': json.loads(body) if body else {},
            }
    except Exception as e:
        return {
            'ok': False,
            'status': None,
            'latency_ms': round((time.time() - started) * 1000, 3),
            'error': str(e),
        }


def tool_mcp_health_report(args: dict) -> dict:
    """Return one-shot MCP health summary for V1, V2, memory maintenance, and git dirtiness."""
    include_git = bool((args or {}).get('include_git', True))
    include_deep = bool((args or {}).get('include_deep', True))
    v1 = _http_json('http://127.0.0.1:3000/health', timeout=3)
    v2 = _http_json('http://127.0.0.1:3001/health', timeout=3)
    v2_deep = _http_json('http://127.0.0.1:3001/health/deep', timeout=10) if include_deep else None
    # Probe layer-2 vision daemons. These don't have to be alive for V1/V2 to
    # work, but their state is operationally relevant (Gemini quota / CDP
    # browser availability frequently silently breaks classify_screen).
    gemini_router = _http_json('http://127.0.0.1:17263/health', timeout=2)
    llama_server = _http_json('http://127.0.0.1:8091/health', timeout=2)
    git_report = tool_git_dirty_report({}) if include_git else None

    v1_json = v1.get('json') or {}
    v2_json = v2.get('json') or {}
    deep_json = (v2_deep or {}).get('json') or {}
    issues = []
    warnings = []
    if not v1.get('ok'):
        issues.append('v1_health_unreachable')
    if not v2.get('ok'):
        issues.append('v2_health_unreachable')
    if include_deep and v2_deep and not v2_deep.get('ok'):
        issues.append('v2_deep_health_unreachable')
    if deep_json.get('memory_db_ok') is False:
        issues.append('memory_db_not_ok')
    if deep_json.get('semantic_lite_ok') is False:
        issues.append('semantic_lite_not_ok')
    if int(deep_json.get('maintenance_recommended_count') or 0) > 0:
        issues.append('maintenance_recommended')
    if git_report and int(git_report.get('source_dirty') or 0) > 0:
        issues.append('source_dirty')
    if v1.get('ok') and 'v2_health_ms' not in v1_json:
        warnings.append('v1_health_payload_outdated')
    if git_report and int(git_report.get('git_warnings') or 0) > 0:
        warnings.append('git_status_warnings')
    if not gemini_router.get('ok'):
        warnings.append('gemini_router_unreachable')
    if not llama_server.get('ok'):
        warnings.append('llama_server_unreachable')

    status = 'ok' if not issues else 'degraded'

    # P0: operator_summary - 1-line natural language summary for humans / commander AI
    def _operator_summary(status, issues, warnings):
        if status == 'down':
            return 'down; immediate action required'
        if status == 'degraded':
            issue_str = ', '.join(issues)
            if warnings:
                warn_str = ', '.join(warnings)
                return f'degraded; action required: {issue_str}; also: {warn_str}'
            return f'degraded; action required: {issue_str}'
        # status == 'ok'
        if not warnings:
            return 'ok; all systems clean'
        if len(warnings) == 1:
            w = warnings[0]
            hints = {
                'v1_health_payload_outdated': 'outdated V1 payload (resolves on next V1 restart)',
                'git_status_warnings': 'git status has warnings (check git_dirty_report)',
                'gemini_router_unreachable': 'gemini_router (:17263) down -- classify_screen will fail',
                'llama_server_unreachable': 'llama-server (:8091) down -- E4B vision unavailable',
            }
            desc = hints.get(w, w)
            return f'ok; warning: {desc}; no action required'
        warn_str = ', '.join(warnings)
        return f'ok; {len(warnings)} warnings ({warn_str}); no action required'

    return {
        'status': status,
        'operator_summary': _operator_summary(status, issues, warnings),
        'issues': issues,
        'warnings': warnings,
        'summary': {
            'v1_alive': bool(v1_json.get('v2_alive')) if v1.get('ok') else False,
            'v1_payload_has_detail': 'v2_health_ms' in v1_json,
            'v2_ok': bool(v2.get('ok') and v2_json.get('status') == 'ok'),
            'v2_tools': v2_json.get('tools'),
            'v2_pid': v2_json.get('pid'),
            'v2_heartbeat_age_sec': v2_json.get('heartbeat_age_sec'),
            'memory_db_ok': deep_json.get('memory_db_ok'),
            'semantic_lite_ok': deep_json.get('semantic_lite_ok'),
            'maintenance_recommended_count': deep_json.get('maintenance_recommended_count'),
            'gemini_router_ok': bool(gemini_router.get('ok')),
            'gemini_router_latency_ms': gemini_router.get('latency_ms'),
            'llama_server_ok': bool(llama_server.get('ok')),
            'llama_server_latency_ms': llama_server.get('latency_ms'),
            'source_dirty': git_report.get('source_dirty') if git_report else None,
            'known_dirty': git_report.get('known_dirty') if git_report else None,
            'known_dirty_policy_ok': (
                git_report.get('source_dirty', 0) == 0
                if git_report is not None else None
            ),
            'generated_dirty': git_report.get('generated_dirty') if git_report else None,
            'scratch_dirty': git_report.get('scratch_dirty') if git_report else None,
            'untracked_dirty': git_report.get('untracked_dirty') if git_report else None,
            'git_warnings': git_report.get('git_warnings') if git_report else None,
        },
        'v1_health': v1,
        'v2_health': v2,
        'v2_deep_health': v2_deep,
        'git_dirty_report': git_report,
    }

# ---------------------------------------------------------------------------
# status (サーバー・デバイス全体状態)
# ---------------------------------------------------------------------------
# V1 check cache to avoid hammering V1 on frequent status polls
_v1_check_cache = {'value': None, 'ts': 0.0}
_V1_CHECK_TTL_SEC = 5.0


def _check_v1_cached():
    """Check V1 /health with short cache. Returns (status, reason_or_None)."""
    now = time.time()
    cached = _v1_check_cache
    if cached['value'] is not None and (now - cached['ts']) < _V1_CHECK_TTL_SEC:
        return cached['value']
    try:
        import requests
        r = requests.get('http://localhost:3000/health', timeout=5)
        if r.status_code == 200:
            result = ('running', None)
        else:
            result = ('error', 'http_' + str(r.status_code))
    except Exception as e:
        name = type(e).__name__
        if 'Timeout' in name:
            result = ('unreachable', 'timeout_5s')
        elif 'ConnectionError' in name or 'Connection' in name:
            result = ('unreachable', 'connection_refused')
        else:
            result = ('unreachable', 'exception_' + name)
    _v1_check_cache['value'] = result
    _v1_check_cache['ts'] = now
    return result


def _init_cpu_meters():
    """Warm up psutil cpu_percent counters so subsequent interval=None calls return real deltas."""
    import psutil, os
    proc = psutil.Process(os.getpid())
    try:
        psutil.cpu_percent(interval=None)  # seed system-wide
        proc.cpu_percent(interval=None)  # seed v2 process
    except Exception:
        pass
    return proc


_v2_proc = _init_cpu_meters()


def tool_status(args: dict) -> dict:
    import psutil
    # system_cpu_percent is INTENTIONALLY OMITTED in this env:
    # V2 runs on Windows Store Python sandbox where psutil.cpu_times().idle
    # counter does not advance, causing cpu_percent() to always return 100.0.
    # This previously caused misdiagnosis (e.g. "V2 占有説" while system actually
    # idle). For system-wide CPU, run externally:
    #   Get-Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 1
    try:
        v2_cpu_raw = _v2_proc.cpu_percent(interval=None)
        cores = max(1, psutil.cpu_count(logical=True) or 1)
        v2_cpu = round(v2_cpu_raw / cores, 1)
    except Exception:
        v2_cpu_raw = 0.0
        v2_cpu = 0.0
    result = {
        'server_v2': 'running',
        'port': 3001,
        'v2_cpu_percent': v2_cpu,
        'v2_cpu_percent_raw_sum': v2_cpu_raw,  # >100 normal on multicore (sum across cores)
        'memory_percent': psutil.virtual_memory().percent,
        'uptime_sec': time.time() - _start_time,
        'cpu_note': (
            'system_cpu_percent omitted (psutil unreliable under Windows Store Python '
            'sandbox: idle counter does not advance). v2_cpu_percent = V2 process only, '
            'normalized to 0-100. For system-wide CPU use Get-Counter externally.'
        ),
    }
    v1_status, v1_reason = _check_v1_cached()
    result['server_v1'] = v1_status
    if v1_reason:
        result['server_v1_reason'] = v1_reason
    return result


_start_time = time.time()

# ---------------------------------------------------------------------------
# restart_server
# ---------------------------------------------------------------------------
def tool_restart_server(args: dict) -> dict:
    force = (args or {}).get('force', False)
    delay = int((args or {}).get('delay', 2) or 2)
    if not force:
        return {'error': 'force=true required'}

    def _do_restart():
        time.sleep(delay)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do_restart, daemon=True).start()
    return {'ok': True, 'message': f'Restarting in {delay}s'}

# ---------------------------------------------------------------------------
# approve (危険操作の承認)
# ---------------------------------------------------------------------------
_pending_approvals: dict = {}
_approvals_lock = threading.Lock()

def tool_approve(args: dict) -> dict:
    """危険操作の承認。operation_idを承認または拒否する。"""
    operation_id = (args or {}).get('operation_id', '')
    approved     = (args or {}).get('approved', False)
    if not operation_id:
        return {'error': 'operation_id required'}
    with _approvals_lock:
        _pending_approvals[operation_id] = approved
    return {'ok': True, 'operation_id': operation_id, 'approved': approved}

def wait_for_approval(operation_id: str, timeout: int = 300) -> bool:
    """承認を待つ（内部使用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _approvals_lock:
            if operation_id in _pending_approvals:
                return _pending_approvals.pop(operation_id)
        time.sleep(1)
    return False

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
        lines = diff_r.stdout.split('\n')
        truncated = len(lines) > max_lines
        if truncated:
            # Keep first max_lines, add summary
            shown = lines[:max_lines]
            omitted = len(lines) - max_lines
            shown.append(f'\n... [{omitted} more lines omitted - use stat_only=true or path= to narrow]')
            diff_out = '\n'.join(shown)
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



# ---------------------------------------------------------------------------
# code_search - Pattern search across source files
# ---------------------------------------------------------------------------
def tool_code_search(args: dict) -> dict:
    """Search for a pattern across source files. Returns file:line:content hits.

    Args:
        pattern:   Regex or literal string to search
        include:   Comma-separated glob patterns (default: *.cpp,*.hpp,*.py,*.kt)
        path:      Root directory to search (default: MIRAGE_DIR)
        context:   Lines of context around each match (default: 0)
        max_hits:  Max results to return (default: 30)
        literal:   bool - treat pattern as literal string not regex (default: False)
    """
    import re as _re

    pattern   = (args or {}).get('pattern', '')
    include   = (args or {}).get('include', '*.cpp,*.hpp,*.py,*.kt')
    root      = (args or {}).get('path', MIRAGE_DIR)
    context   = int((args or {}).get('context', 0) or 0)
    max_hits  = int((args or {}).get('max_hits', 30) or 30)
    literal   = bool((args or {}).get('literal', False))

    if not pattern:
        return {'error': 'pattern required'}

    # Build file list via glob
    import glob as _glob
    patterns = [p.strip() for p in include.split(',')]
    files = []
    for pat in patterns:
        files.extend(_glob.glob(
            os.path.join(root, '**', pat), recursive=True
        ))
    # Exclude build dirs, __pycache__, .git
    skip = {'.git', 'build', 'Debug', 'Release', '__pycache__', '.vs', 'out'}
    files = [f for f in files if not any(s in f.split(os.sep) for s in skip)]

    if literal:
        search_str = _re.escape(pattern)
    else:
        search_str = pattern

    try:
        regex = _re.compile(search_str, _re.IGNORECASE)
    except _re.error as e:
        return {'error': f'invalid regex: {e}'}

    hits = []
    for fpath in sorted(files):
        try:
            lines = open(fpath, 'r', encoding='utf-8', errors='replace').readlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if regex.search(line):
                entry = {
                    'file': os.path.relpath(fpath, root).replace('\\', '/'),
                    'line': i + 1,
                    'text': line.rstrip(),
                }
                if context > 0:
                    ctx_lines = []
                    for ci in range(max(0, i - context), min(len(lines), i + context + 1)):
                        prefix = '>' if ci == i else ' '
                        ctx_lines.append(f'{prefix} {ci+1}: {lines[ci].rstrip()}')
                    entry['context'] = '\n'.join(ctx_lines)
                hits.append(entry)
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            break

    return {
        'hits': hits,
        'count': len(hits),
        'truncated': len(hits) >= max_hits,
        'pattern': pattern,
        'files_searched': len(files),
    }


# ---------------------------------------------------------------------------
# build_and_report - Run cmake build and return structured error report
# ---------------------------------------------------------------------------
def tool_build_and_report(args: dict) -> dict:
    """Run cmake --build and return structured error/warning report.

    Parses MSVC/GCC/Clang output into structured errors with file/line/message.

    Args:
        build_dir:   cmake build directory (default: MIRAGE_DIR/build)
        target:      cmake target (default: all)
        config:      Release|Debug (default: Debug)
        max_errors:  max errors to return (default: 20)
        jobs:        parallel jobs (default: 4)
    """
    import re as _re

    build_dir  = (args or {}).get('build_dir',
                   os.path.join(MIRAGE_DIR, 'build'))
    target     = (args or {}).get('target', '')
    config     = (args or {}).get('config', 'Debug')
    max_errors = int((args or {}).get('max_errors', 20) or 20)
    jobs       = int((args or {}).get('jobs', 4) or 4)

    # target / config go into shell=True command; whitelist to prevent injection.
    _name_re = _re.compile(r'^[A-Za-z0-9_\-]+$')
    if target and not _name_re.match(str(target)):
        return {'ok': False, 'error': f'invalid target {target!r}; must match ^[A-Za-z0-9_\\-]+$'}
    if not _name_re.match(str(config)):
        return {'ok': False, 'error': f'invalid config {config!r}; must match ^[A-Za-z0-9_\\-]+$'}

    cmd = f'cmake --build . --config {config} --parallel {jobs}'
    if target:
        cmd += f' --target {target}'

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=300, cwd=build_dir, encoding='utf-8', errors='replace',
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'build timeout after 300s'}
    except FileNotFoundError:
        return {'ok': False, 'error': f'build_dir not found: {build_dir}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

    combined = (result.stdout or '') + '\n' + (result.stderr or '')
    lines = combined.split('\n')

    # Parse errors and warnings
    # MSVC:  file.cpp(42): error C2065: ...
    # GCC:   file.cpp:42:10: error: ...
    # Ninja: FAILED: path/to/file.cpp.obj
    error_pat = _re.compile(
        r'(?P<file>[^\s(]+?[.](cpp|hpp|c|h|py|kt))'
        r'[:(](?P<line>\d+)[):,]'
        r'.*?(?P<sev>error|warning|note)\s*(?:[A-Z]\d+)?:\s*'
        r'(?P<msg>.+)',
        _re.IGNORECASE
    )

    errors = []
    warnings = []
    failed_files = []

    for line in lines:
        m = error_pat.search(line)
        if m:
            entry = {
                'file': os.path.relpath(m.group('file'), build_dir)
                        if os.path.isabs(m.group('file')) else m.group('file'),
                'line': int(m.group('line')),
                'severity': m.group('sev').lower(),
                'message': m.group('msg').strip()[:200],
            }
            if entry['severity'] == 'error':
                errors.append(entry)
            else:
                warnings.append(entry)
        elif 'FAILED:' in line:
            fname = line.replace('FAILED:', '').strip()
            if fname:
                failed_files.append(fname)

    # Build summary
    ok = result.returncode == 0
    return {
        'ok': ok,
        'exit_code': result.returncode,
        'errors': errors[:max_errors],
        'errors_count': len(errors),
        'warnings_count': len(warnings),
        'warnings': warnings[:5],  # top 5 warnings
        'failed_files': failed_files[:10],
        'log_tail': '\n'.join(lines[-30:]) if not ok else '',
        'build_dir': build_dir,
        'config': config,
    }

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
TOOLS = {
    'code_search': {
        'description': 'Search pattern across source files. Returns file:line:text hits with optional context lines.',
        'schema': {'type': 'object', 'properties': {
            'pattern':  {'type': 'string', 'description': 'Regex or literal search pattern'},
            'include':  {'type': 'string', 'description': 'Comma-separated globs (default: *.cpp,*.hpp,*.py,*.kt)'},
            'path':     {'type': 'string', 'description': 'Root directory'},
            'context':  {'type': 'integer', 'description': 'Lines of context (default 0)'},
            'max_hits': {'type': 'integer', 'description': 'Max results (default 30)'},
            'literal':  {'type': 'boolean', 'description': 'Literal string match (default False)'},
        }, 'required': ['pattern']},
        'handler': tool_code_search,
    },
    'build_and_report': {
        'description': 'Run cmake --build and return structured {ok, errors:[{file,line,severity,message}], warnings_count, log_tail}.',
        'schema': {'type': 'object', 'properties': {
            'build_dir':  {'type': 'string', 'description': 'cmake build directory'},
            'target':     {'type': 'string', 'description': 'cmake target (default: all)'},
            'config':     {'type': 'string', 'description': 'Release|Debug (default: Debug)'},
            'max_errors': {'type': 'integer', 'description': 'Max errors in response (default 20)'},
            'jobs':       {'type': 'integer', 'description': 'Parallel jobs (default 4)'},
        }},
        'handler': tool_build_and_report,
    },
    'run_command': {
        'description': 'Run a shell command in the workspace.',
        'schema': {'type': 'object', 'properties': {
            'command': {'type': 'string'},
            'cwd': {'type': 'string'},
            'timeout': {'type': 'integer'},
        }, 'required': ['command']},
        'handler': tool_run_command,
    },
    'read_file': {
        'description': 'Read file contents.',
        'schema': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
        }, 'required': ['path']},
        'handler': tool_read_file,
    },
    'write_file': {
        'description': 'Write content to file.',
        'schema': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'content': {'type': 'string'},
        }, 'required': ['path', 'content']},
        'handler': tool_write_file,
    },
    'write_file_b64': {
        'description': 'Write file from base64 data with chunked append support.',
        'schema': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'data_b64': {'type': 'string'},
            'mode': {'type': 'string', 'enum': ['overwrite', 'append']},
        }, 'required': ['path', 'data_b64']},
        'handler': tool_write_file_b64,
    },
    'list_files': {
        'description': 'List files in directory.',
        'schema': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'pattern': {'type': 'string'},
        }},
        'handler': tool_list_files,
    },
    'git_diff': {
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
    'git_status': {
        'description': 'Get git repository status.',
        'schema': {'type': 'object', 'properties': {
            'cwd': {'type': 'string'},
        }},
        'handler': tool_git_status,
    },
    'git_dirty_report': {
        'description': 'Classify git dirty files into source changes, generated cache, pycache, runtime artifacts, and scratch files.',
        'schema': {'type': 'object', 'properties': {
            'repos': {'type': 'array', 'items': {'type': 'string'}},
        }},
        'handler': tool_git_dirty_report,
    },
    'mcp_health_report': {
        'description': 'One-shot MCP health summary: V1/V2 health, V2 deep diagnostics, maintenance recommendation, and git dirty classification.',
        'schema': {'type': 'object', 'properties': {
            'include_git': {'type': 'boolean'},
            'include_deep': {'type': 'boolean'},
        }},
        'handler': tool_mcp_health_report,
    },
    'status': {
        'description': 'Get MirageSystem overall status.',
        'schema': {'type': 'object', 'properties': {}},
        'handler': tool_status,
    },
    'restart_server': {
        'description': 'Restart MCP server. force=true required.',
        'schema': {'type': 'object', 'properties': {
            'force': {'type': 'boolean'},
            'delay': {'type': 'integer'},
        }},
        'handler': tool_restart_server,
    },
    'approve': {
        'description': 'Approve or reject a pending dangerous operation.',
        'schema': {'type': 'object', 'properties': {
            'operation_id': {'type': 'string'},
            'approved': {'type': 'boolean'},
        }, 'required': ['operation_id']},
        'handler': tool_approve,
    },
}
