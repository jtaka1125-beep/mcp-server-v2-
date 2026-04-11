#!/usr/bin/env python3
"""
fix_pid_lock.py
問題: check_pid_alive が PID を見ずに 'python' 文字列だけ判定
→ 全 python プロセスが生きている限り「別インスタンス起動済み」と誤検知
再発防止: PID 一致 + ポート疎通の二段階チェックに変更
"""
import sys, os

path = r'C:\MirageWork\mcp-server-v2\server.py'
with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

old_check = '''def check_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is running (Windows compatible)"""
    import subprocess
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
            capture_output=True, text=True, timeout=5
        )
        return 'python' in result.stdout.lower()
    except Exception:
        return False'''

new_check = '''def check_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is running (Windows compatible).
    Uses exact PID match to avoid false positives from other python processes.
    """
    import subprocess
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'],
            capture_output=True, text=True, timeout=5
        )
        # CSV output: "python3.exe","12345","Console","1","xx MB"
        # Check that the exact PID appears in the output
        return f'"{pid}"' in result.stdout
    except Exception:
        return False

def check_port_alive(port: int) -> bool:
    """Check if something is actually listening on the port."""
    import socket
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False'''

old_acquire = '''def acquire_lock() -> bool:
    """Acquire PID lock. Returns False if another instance is running."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            if check_pid_alive(old_pid):
                return False  # Another instance is running
        except (ValueError, IOError):
            pass  # Invalid PID file, overwrite it
    
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True'''

new_acquire = '''def acquire_lock() -> bool:
    """Acquire PID lock. Returns False if another instance is running.
    Two-stage check: PID alive AND port responding.
    """
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid() and check_pid_alive(old_pid) and check_port_alive(PORT_NEW):
                log.warning(f'Active instance found: PID={old_pid} port={PORT_NEW}')
                return False
        except (ValueError, IOError):
            pass  # Invalid PID file, overwrite it
    
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True'''

if old_check in code and old_acquire in code:
    code = code.replace(old_check, new_check).replace(old_acquire, new_acquire)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(code)
    print('OK: PID lock logic fixed')
    print('  - check_pid_alive: exact PID match via CSV')
    print('  - acquire_lock: PID alive AND port alive (two-stage)')
else:
    print('Pattern not found, checking what exists...')
    if 'check_pid_alive' in code:
        idx = code.find('def check_pid_alive')
        print(repr(code[idx:idx+300]))
