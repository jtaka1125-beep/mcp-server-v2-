#!/usr/bin/env python3
"""
fix_v2_server.py
- RotatingFileHandler の構文修正
- do_GET に /api/* フォワーディング追加  
- 未知セッション警告の抑制
"""
import sys, os

v2_path = r'C:\MirageWork\mcp-server-v2\server.py'
with open(v2_path, 'r', encoding='utf-8', errors='replace') as f:
    code = f.read()

# ---- Fix 1: Broken RotatingFileHandler ----
broken = """        logging.handlers.RotatingFileHandler(
            os.path.join(os.path.dirname(__file__, maxBytes=5*1024*1024, backupCount=3), 'logs', 'server.log'),
            encoding='utf-8',
        ),"""

fixed = """        logging.handlers.RotatingFileHandler(
            os.path.join(os.path.dirname(__file__), 'logs', 'server.log'),
            maxBytes=5*1024*1024, backupCount=3, encoding='utf-8',
        ),"""

if broken in code:
    code = code.replace(broken, fixed)
    print('Fix1 OK: RotatingFileHandler syntax fixed')
else:
    # Maybe it was already broken differently - let's check
    if 'os.path.dirname(__file__, maxBytes' in code:
        import re
        code = re.sub(
            r"os\.path\.join\(os\.path\.dirname\(__file__, [^)]+\), 'logs', 'server\.log'\)",
            "os.path.join(os.path.dirname(__file__), 'logs', 'server.log')",
            code
        )
        # Fix maxBytes - ensure it's a parameter of RotatingFileHandler not dirname
        code = code.replace(
            "RotatingFileHandler(\n            os.path.join(os.path.dirname(__file__), 'logs', 'server.log'),\n            encoding='utf-8',",
            "RotatingFileHandler(\n            os.path.join(os.path.dirname(__file__), 'logs', 'server.log'),\n            maxBytes=5*1024*1024, backupCount=3, encoding='utf-8',"
        )
        print('Fix1 OK: Used regex fix')
    else:
        print('Fix1 SKIP: pattern not found, checking current state...')
        # Check current state
        idx = code.find('RotatingFileHandler')
        if idx >= 0:
            print('  Current:', repr(code[idx:idx+200]))

# ---- Fix 2: do_GET /api/* forwarding ----
old_get_404 = """        else:
            self._send_json(404, {'error': 'not found'})"""

new_get_404 = """        elif self.path.startswith('/api/'):
            # Forward /api/* to legacy server (has memory/bootstrap etc)
            self._proxy_api_get(self.path)
        else:
            self._send_json(404, {'error': 'not found'})"""

if '_proxy_api_get' not in code:
    code = code.replace(old_get_404, new_get_404)

    proxy_method = """
    def _proxy_api_get(self, path: str):
        \"\"\"Forward GET /api/* to legacy mcp-server at port 3000\"\"\"
        import urllib.request
        legacy_url = 'http://localhost:3000'
        try:
            with urllib.request.urlopen(f'{legacy_url}{path}', timeout=10) as resp:
                data = resp.read()
                self.send_response(200)
                ct = resp.headers.get('Content-Type', 'application/json')
                self.send_header('Content-Type', ct)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self._send_json(502, {'error': str(e)})

"""
    # Insert before do_POST
    code = code.replace('    def do_POST(self):', proxy_method + '    def do_POST(self):')
    print('Fix2 OK: _proxy_api_get added')
else:
    print('Fix2 SKIP: already patched')

# ---- Fix 3: Suppress unknown session warnings (cosmetic) ----
# Already handled in V2 since it doesn't have session management

with open(v2_path, 'w', encoding='utf-8') as f:
    f.write(code)

print('\nV2 server.py patched.')

# ---- Fix slim proxy: add /api/* forwarding from Cloudflare side ----
# The slim proxy on 3000 now forwards /api/* to V2 on 3001
# V2 on 3001 now also has /api/* forwarding back to 3000 legacy endpoints
# This creates a potential loop - let's check and fix

slim_path = r'C:\MirageWork\mcp-server\server.py'
with open(slim_path, 'r', encoding='utf-8', errors='replace') as f:
    slim = f.read()

# Fix: slim proxy forwards /api/* to V2 (3001)
# but V2's memory/bootstrap is directly implemented in V2
# Check if V2 has memory_bootstrap via /api/v1/memory/bootstrap
# Looking at V2 tools, memory_bootstrap is a tool not an HTTP endpoint
# So we need to add HTTP endpoints to V2 for legacy API routes

print('\nSlim proxy size:', len(slim))
print('Has _forward_get_to_v2:', '_forward_get_to_v2' in slim)
