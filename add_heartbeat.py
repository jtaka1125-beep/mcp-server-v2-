#!/usr/bin/env python3
"""add_heartbeat.py - V2 server に heartbeat スレッドを追加"""
import sys

path = r'C:\MirageWork\mcp-server-v2\server.py'
with open(path, 'r', encoding='utf-8') as f:
    code = f.read()

if 'heartbeat' in code.lower():
    print('Already has heartbeat')
    sys.exit(0)

# Add heartbeat thread import
code = code.replace(
    'import json\nimport logging',
    'import json\nimport logging\nimport threading'
)

# Add heartbeat function + start before server.serve_forever()
hb_func = '''
HEARTBEAT_FILE = os.path.join(os.path.dirname(__file__), 'server.heartbeat')

def _heartbeat_loop():
    """Write heartbeat file every 30s so watchdog knows we're alive."""
    while True:
        try:
            with open(HEARTBEAT_FILE, 'w') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
        threading.Event().wait(30)

'''

# Insert before PID_FILE definition
code = code.replace(
    "PID_FILE = os.path.join(os.path.dirname(__file__), 'server.pid')",
    hb_func + "PID_FILE = os.path.join(os.path.dirname(__file__), 'server.pid')"
)

# Start heartbeat thread before serve_forever
code = code.replace(
    "        server = HTTPServer(('0.0.0.0', PORT_NEW), MCPHandler)\n        log.info(f'mcp-server-v2 listening on port {PORT_NEW}')",
    "        hb = threading.Thread(target=_heartbeat_loop, daemon=True)\n        hb.start()\n        server = HTTPServer(('0.0.0.0', PORT_NEW), MCPHandler)\n        log.info(f'mcp-server-v2 listening on port {PORT_NEW}')"
)

# Cleanup heartbeat on exit
code = code.replace(
    "        release_lock()\n        try:",
    "        release_lock()\n        try:\n            os.remove(HEARTBEAT_FILE)\n        except OSError:\n            pass\n        try:"
)

with open(path, 'w', encoding='utf-8') as f:
    f.write(code)

print('OK: heartbeat thread added')
print('  - writes server.heartbeat every 30s')
print('  - guard checks heartbeat age < 60s')
