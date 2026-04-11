#!/usr/bin/env python3
"""
implement_5tools.py - 5つの新ツールを実装する
1. code_search        → tools/system.py
2. build_and_report   → tools/system.py
3. device_health      → tools/device.py
4. session_checkpoint → tools/memory.py
5. memory_diff        → tools/memory.py
"""
import os

BASE = r'C:\MirageWork\mcp-server-v2'

# ============================================================
# 1 & 2. code_search + build_and_report → tools/system.py
# ============================================================
def patch_system_py():
    path = os.path.join(BASE, 'tools', 'system.py')
    content = open(path, 'r', encoding='utf-8').read()

    new_tools_code = '''
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
                    'file': os.path.relpath(fpath, root).replace('\\\\', '/'),
                    'line': i + 1,
                    'text': line.rstrip(),
                }
                if context > 0:
                    ctx_lines = []
                    for ci in range(max(0, i - context), min(len(lines), i + context + 1)):
                        prefix = '>' if ci == i else ' '
                        ctx_lines.append(f'{prefix} {ci+1}: {lines[ci].rstrip()}')
                    entry['context'] = '\\n'.join(ctx_lines)
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

    combined = (result.stdout or '') + '\\n' + (result.stderr or '')
    lines = combined.split('\\n')

    # Parse errors and warnings
    # MSVC:  file.cpp(42): error C2065: ...
    # GCC:   file.cpp:42:10: error: ...
    # Ninja: FAILED: path/to/file.cpp.obj
    error_pat = _re.compile(
        r'(?P<file>[^\\s(]+?[.](cpp|hpp|c|h|py|kt))'
        r'[:(](?P<line>\\d+)[):,]'
        r'.*?(?P<sev>error|warning|note)\\s*(?:[A-Z]\\d+)?:\\s*'
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
        'log_tail': '\\n'.join(lines[-30:]) if not ok else '',
        'build_dir': build_dir,
        'config': config,
    }

'''

    # Insert before TOOLS = {
    marker = '# ---------------------------------------------------------------------------\n# ツール登録テーブル'
    if 'def tool_code_search(' not in content:
        content = content.replace(marker, new_tools_code + marker)
        print('✓ code_search + build_and_report functions added')

    # Add to TOOLS dict
    new_entries = """    'code_search': {
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
"""
    if "'code_search'" not in content:
        content = content.replace(
            "    'run_command': {",
            new_entries + "    'run_command': {"
        )
        print('✓ code_search + build_and_report added to TOOLS')

    open(path, 'w', encoding='utf-8').write(content)
    print('✓ system.py done')


# ============================================================
# 3. device_health → tools/device.py
# ============================================================
def patch_device_py():
    path = os.path.join(BASE, 'tools', 'device.py')
    content = open(path, 'r', encoding='utf-8').read()

    device_health_func = '''
# ---------------------------------------------------------------------------
# device_health - One-shot device health check
# ---------------------------------------------------------------------------
def tool_device_health(args: dict) -> dict:
    """Check device health in one call: WiFi ADB, TCP port reachability,
    APK running, battery level, screen state, RNDIS IP.

    Args:
        device:     WiFi ADB address (e.g. 192.168.0.10:5555)
        tcp_host:   USBLAN/WiFi IP for TCP port check
        tcp_port:   Video TCP port (e.g. 50000)
        apk_pkg:    APK package name (default: com.mirage.capture)
    """
    import socket as _socket
    import subprocess as _sub

    device   = (args or {}).get('device', '')
    tcp_host = (args or {}).get('tcp_host', '')
    tcp_port = int((args or {}).get('tcp_port', 0) or 0)
    apk_pkg  = (args or {}).get('apk_pkg', 'com.mirage.capture')
    adb_exe  = r'C:\\Users\\jun\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe'

    result = {
        'device': device,
        'wifi_adb': False,
        'tcp_reachable': None,
        'apk_running': False,
        'battery': None,
        'screen_on': None,
        'rndis_ip': None,
        'errors': [],
    }

    if not device:
        return {'error': 'device required (e.g. 192.168.0.10:5555)'}

    def _adb(cmd, timeout=5):
        try:
            r = _sub.run(
                [adb_exe, '-s', device, 'shell'] + cmd.split(),
                capture_output=True, text=True,
                timeout=timeout, encoding='utf-8', errors='replace',
            )
            return r.stdout.strip(), r.returncode == 0
        except Exception as e:
            return str(e), False

    # 1. WiFi ADB connectivity
    try:
        r = _sub.run(
            [adb_exe, 'connect', device],
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace',
        )
        result['wifi_adb'] = 'connected' in r.stdout.lower() or 'already' in r.stdout.lower()
    except Exception as e:
        result['errors'].append(f'adb_connect: {e}')

    if result['wifi_adb']:
        # 2. Battery
        out, ok = _adb('dumpsys battery')
        if ok:
            for line in out.split('\\n'):
                if 'level:' in line.lower():
                    try:
                        result['battery'] = int(line.split(':')[1].strip())
                    except Exception:
                        pass
                    break

        # 3. Screen state
        out, ok = _adb('dumpsys power')
        if ok:
            result['screen_on'] = 'mWakefulness=Awake' in out or 'Display Power: state=ON' in out

        # 4. APK running
        out, ok = _adb(f'pidof {apk_pkg}')
        result['apk_running'] = ok and bool(out.strip())

        # 5. RNDIS IP (rndis0 or usb0)
        for iface in ('rndis0', 'usb0', 'rndis1'):
            out, ok = _adb(f'ip addr show {iface}')
            if ok and 'inet ' in out:
                import re as _re
                m = _re.search(r'inet (\\d+\\.\\d+\\.\\d+\\.\\d+)', out)
                if m:
                    result['rndis_ip'] = m.group(1)
                    result['rndis_iface'] = iface
                    break

    # 6. TCP port reachability (independent of ADB)
    if tcp_host and tcp_port:
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(2)
            r = sock.connect_ex((tcp_host, tcp_port))
            sock.close()
            result['tcp_reachable'] = (r == 0)
        except Exception as e:
            result['tcp_reachable'] = False
            result['errors'].append(f'tcp: {e}')

    # Overall health score
    checks = [result['wifi_adb'], result['apk_running']]
    if result['tcp_reachable'] is not None:
        checks.append(result['tcp_reachable'])
    passed = sum(1 for c in checks if c)
    result['health'] = 'GREEN' if passed == len(checks) else \\
                       'YELLOW' if passed > 0 else 'RED'
    result['health_score'] = f'{passed}/{len(checks)}'

    return result

'''

    # Insert before TOOLS =
    tools_marker = '\nTOOLS = {'
    if 'def tool_device_health(' not in content:
        content = content.replace(tools_marker, device_health_func + tools_marker)
        print('✓ device_health function added')

    # Add to TOOLS
    health_entry = """    'device_health': {
        'description': 'One-shot device health check: WiFi ADB + TCP port + APK running + battery + screen + RNDIS IP. Returns health=GREEN/YELLOW/RED.',
        'schema': {'type': 'object', 'properties': {
            'device':   {'type': 'string', 'description': 'WiFi ADB address (e.g. 192.168.0.10:5555)'},
            'tcp_host': {'type': 'string', 'description': 'IP for TCP port check'},
            'tcp_port': {'type': 'integer', 'description': 'TCP video port (e.g. 50000)'},
            'apk_pkg':  {'type': 'string',  'description': 'APK package (default: com.mirage.capture)'},
        }, 'required': ['device']},
        'handler': tool_device_health,
    },
"""
    if "'device_health'" not in content:
        # Insert at start of TOOLS
        content = content.replace(
            "TOOLS = {\n",
            "TOOLS = {\n" + health_entry
        )
        print('✓ device_health added to TOOLS')

    open(path, 'w', encoding='utf-8').write(content)
    print('✓ device.py done')


# ============================================================
# 4 & 5. session_checkpoint + memory_diff → tools/memory.py
# ============================================================
def patch_memory_py():
    path = os.path.join(BASE, 'tools', 'memory.py')
    content = open(path, 'r', encoding='utf-8').read()

    new_memory_tools = '''

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
    git_cwd   = (args or {}).get('git_cwd', r'C:\\MirageWork\\MirageVulkan')
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
            git_summary = f'Git commits (last 24h):\\n{commits}'
    except Exception:
        pass

    # 2. Build checkpoint content
    parts = [f'# Session Checkpoint {timestamp}']
    if done:
        parts.append(f'\\n## 完了\\n{done}')
    if git_summary:
        parts.append(f'\\n## {git_summary}')
    if next_acts:
        items = next_acts if isinstance(next_acts, list) else [next_acts]
        parts.append('\\n## 次のアクション\\n' + '\\n'.join(f'- {a}' for a in items))
    if issues:
        items = issues if isinstance(issues, list) else [issues]
        parts.append('\\n## 未解決\\n' + '\\n'.join(f'- {i}' for i in items))

    checkpoint_text = '\\n'.join(parts)

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
        md_path = r'C:\\MirageWork\\MirageVulkan\\PROJECT_STATE.md'
        try:
            md = open(md_path, 'r', encoding='utf-8').read()

            # Update "Last Updated" line or prepend checkpoint section
            checkpoint_block = (
                f'\\n## Last Session ({timestamp})\\n'
                + (f'**Done**: {done[:200]}\\n' if done else '')
                + ('**Next**: ' + ', '.join(str(a) for a in next_acts[:3]) + '\\n' if next_acts else '')
                + ('**Blockers**: ' + ', '.join(str(i) for i in issues[:3]) + '\\n' if issues else '')
            )

            # Replace existing "Last Session" block if present
            if '## Last Session' in md:
                md = _re.sub(
                    r'## Last Session.*?(?=\\n## |\Z)',
                    checkpoint_block.strip() + '\\n',
                    md, flags=_re.DOTALL
                )
            else:
                # Prepend after first heading
                first_h2 = md.find('\\n## ')
                if first_h2 >= 0:
                    md = md[:first_h2] + checkpoint_block + md[first_h2:]
                else:
                    md = checkpoint_block + '\\n' + md

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
    db = r'C:\\MirageWork\\mcp-server\\data\\memory.db'

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

'''

    tools_marker = '\nTOOLS = {'
    if 'def tool_session_checkpoint(' not in content:
        content = content.replace(tools_marker, new_memory_tools + tools_marker)
        print('✓ session_checkpoint + memory_diff functions added')

    # Add to TOOLS dict
    new_tool_entries = """    'session_checkpoint': {
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
"""
    if "'session_checkpoint'" not in content:
        content = content.replace(
            "    'active_context': {",
            new_tool_entries + "    'active_context': {"
        )
        print('✓ session_checkpoint + memory_diff added to TOOLS')

    open(path, 'w', encoding='utf-8').write(content)
    print('✓ memory.py done')


# ============================================================
# Run
# ============================================================
if __name__ == '__main__':
    patch_system_py()
    patch_device_py()
    patch_memory_py()
    print('\nAll 5 tools implemented!')
