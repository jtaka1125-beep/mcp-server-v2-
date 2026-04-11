#!/usr/bin/env python3
"""
MCPサーバーV2 一括パッチスクリプト
#2: memory_decision_auto importance バリデーション
#3: detect_popup time UnboundLocalError 確認
#4: run_command No closing quotation 確認
"""
import sys, os, re

def patch_file(path, old, new, desc):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    if old not in content:
        print(f'  SKIP ({desc}): pattern not found')
        return False
    content2 = content.replace(old, new)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content2)
    print(f'  OK ({desc}): replaced {content.count(old)} occurrence(s)')
    return True

# ============================================================
# #2: memory.py - safe importance conversion
# ============================================================
print('\n[#2] Patching memory.py importance validation...')
mem_path = r'C:\MirageWork\mcp-server-v2\tools\memory.py'
with open(mem_path, 'r', encoding='utf-8', errors='replace') as f:
    mem = f.read()

helper = '''
def _safe_int(val, default=3):
    """Convert LLM importance value to int (handles 'High', 'medium', mojibake, etc)."""
    try:
        return max(1, min(5, int(val)))
    except (TypeError, ValueError):
        _map = {'low': 1, 'medium': 3, 'normal': 3, 'high': 4, 'critical': 5}
        return _map.get(str(val).strip().lower(), default)

'''

# Insert helper before first 'def tool_'
first_def = mem.find('\ndef tool_')
if first_def < 0:
    first_def = mem.find('def tool_')
if '_safe_int' not in mem:
    mem = mem[:first_def+1] + helper + mem[first_def+1:]
    print('  Added _safe_int helper')
else:
    print('  _safe_int already present')

# Replace all int(...importance...) patterns
patterns = [
    ("int((args or {}).get('importance', 3) or 3)", "_safe_int((args or {}).get('importance', 3), 3)"),
    ("int(item.get('importance', 3))", "_safe_int(item.get('importance', 3), 3)"),
    ("int((args or {}).get('importance', 3))", "_safe_int((args or {}).get('importance', 3), 3)"),
]
for old, new in patterns:
    if old in mem:
        mem = mem.replace(old, new)
        print(f'  Replaced: {old[:50]}')

with open(mem_path, 'w', encoding='utf-8') as f:
    f.write(mem)
print('  _safe_int count:', mem.count('_safe_int'))

# ============================================================
# #3: vision.py - detect_popup time UnboundLocalError
# ============================================================
print('\n[#3] Checking vision.py detect_popup...')
vis_path = r'C:\MirageWork\mcp-server-v2\tools\vision.py'
with open(vis_path, 'r', encoding='utf-8', errors='replace') as f:
    vis = f.read()

# Check if 'time' is used inside tool_detect_popup as a local variable
func_start = vis.find('def tool_detect_popup')
func_end = vis.find('\ndef tool_', func_start + 1)
if func_end < 0:
    func_end = len(vis)
func_body = vis[func_start:func_end]
if 'time' in func_body:
    print('  WARNING: "time" referenced in detect_popup body, checking...')
    print('  Lines with time:', [l for l in func_body.split('\n') if 'time' in l])
    # Fix: make sure 'import time' is at module level and not redefined locally
else:
    print('  OK: no time reference in detect_popup body')

# Ensure 'import time' at module top
if not vis.startswith('import time') and '\nimport time\n' not in vis[:200]:
    vis = 'import time\n' + vis
    with open(vis_path, 'w', encoding='utf-8') as f:
        f.write(vis)
    print('  Added: import time at top')
else:
    print('  OK: import time present')

# ============================================================
# #5: slim proxy - add /api/v1/memory/bootstrap forwarding
# ============================================================
print('\n[#5] Patching slim proxy server.py...')
slim_path = r'C:\MirageWork\mcp-server\server.py'
with open(slim_path, 'r', encoding='utf-8', errors='replace') as f:
    slim = f.read()

# Add /api/* GET forwarding to V2
old_do_get_end = '        else:\n            self._send_json({"name": SERVER_NAME, "status": "ok", "port": PORT})'
new_do_get_end = '''        elif self.path.startswith("/api/"):
            # Forward all /api/* requests to V2
            self._forward_get_to_v2(self.path)
        else:
            self._send_json({"name": SERVER_NAME, "status": "ok", "port": PORT})'''

if '_forward_get_to_v2' not in slim:
    slim = slim.replace(old_do_get_end, new_do_get_end)

    # Add _forward_get_to_v2 method before _proxy_to_v2
    forward_method = '''
    def _forward_get_to_v2(self, path: str):
        """Forward GET /api/* to V2 at port 3001"""
        try:
            with urllib.request.urlopen(f"{V2_URL}{path}", timeout=15) as resp:
                result = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 502)

'''
    slim = slim.replace('    def _proxy_to_v2', forward_method + '    def _proxy_to_v2')
    with open(slim_path, 'w', encoding='utf-8') as f:
        f.write(slim)
    print('  OK: added _forward_get_to_v2')
else:
    print('  SKIP: already patched')

# ============================================================
# #8: Log rotation - V2 server
# ============================================================
print('\n[#8] Checking V2 server.py log setup...')
v2_srv = r'C:\MirageWork\mcp-server-v2\server.py'
with open(v2_srv, 'r', encoding='utf-8', errors='replace') as f:
    v2 = f.read()

if 'RotatingFileHandler' not in v2:
    old_log = 'logging.FileHandler'
    new_log = 'logging.handlers.RotatingFileHandler'
    if old_log in v2:
        header = 'import logging.handlers\n'
        if 'import logging.handlers' not in v2:
            v2 = v2.replace('import logging\n', 'import logging\nimport logging.handlers\n', 1)
        # Replace FileHandler with RotatingFileHandler(maxBytes=5MB, backupCount=3)
        v2 = re.sub(
            r'logging\.FileHandler\(([^)]+)\)',
            r'logging.handlers.RotatingFileHandler(\1, maxBytes=5*1024*1024, backupCount=3)',
            v2
        )
        with open(v2_srv, 'w', encoding='utf-8') as f:
            f.write(v2)
        print('  OK: RotatingFileHandler added to V2 server')
    else:
        print('  SKIP: no FileHandler found')
else:
    print('  Already has RotatingFileHandler')

# Same for mcp-server (slim) - but it has no file logging
# Check old server for log rotation
old_srv_log = r'C:\MirageWork\mcp-server\server_legacy.py'

print('\nAll patches complete.')
