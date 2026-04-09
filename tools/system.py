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

# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------
def tool_run_command(args: dict) -> dict:
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
    if not path:
        return {'error': 'path required'}
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
    if not path:
        return {'error': 'path required'}
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
    if not path or not data_b64:
        return {'error': 'path and data_b64 required'}
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

# ---------------------------------------------------------------------------
# status (サーバー・デバイス全体状態)
# ---------------------------------------------------------------------------
def tool_status(args: dict) -> dict:
    import psutil
    result = {
        'server_v2': 'running',
        'port': 3001,
        'cpu_percent': psutil.cpu_percent(interval=0.1),
        'memory_percent': psutil.virtual_memory().percent,
        'uptime_sec': time.time() - _start_time,
    }
    # 旧サーバーの生存確認
    try:
        import requests
        r = requests.get('http://localhost:3000/health', timeout=2)
        result['server_v1'] = 'running' if r.status_code == 200 else 'error'
    except Exception:
        result['server_v1'] = 'unreachable'
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
