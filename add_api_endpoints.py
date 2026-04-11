#!/usr/bin/env python3
"""
add_api_endpoints.py
V2 server.py に /api/v1/* HTTP エンドポイントを追加
(memory/bootstrap, memory/search, status など)
"""
import sys, os

v2_path = r'C:\MirageWork\mcp-server-v2\server.py'
with open(v2_path, 'r', encoding='utf-8', errors='replace') as f:
    code = f.read()

if '_handle_api_route' in code:
    print('Already patched')
    sys.exit(0)

# Add API route handler
api_handler = '''
    def _handle_api_route(self, path: str):
        """Handle /api/v1/* REST endpoints"""
        import urllib.parse
        parsed = urllib.parse.urlparse(path)
        qs = urllib.parse.parse_qs(parsed.query)
        seg = parsed.path.lstrip('/').split('/')  # ['api', 'v1', 'memory', 'bootstrap']

        if len(seg) < 3:
            self._send_json(404, {'error': 'not found'})
            return

        section = seg[2] if len(seg) > 2 else ''
        action  = seg[3] if len(seg) > 3 else ''
        ns = qs.get('namespace', ['mirage-vulkan'])[0]

        try:
            if section == 'memory':
                import tools.memory as mem_tools
                from memory import store as mem_store
                if action == 'bootstrap':
                    result = mem_tools.TOOLS['memory_bootstrap']['handler']({'namespace': ns})
                    self._send_json(200, result)
                elif action == 'search':
                    q = qs.get('q', [''])[0]
                    result = mem_tools.TOOLS['memory_search']['handler']({'namespace': ns, 'query': q})
                    self._send_json(200, result)
                elif action == 'append_raw':
                    self._send_json(405, {'error': 'use POST'})
                else:
                    self._send_json(404, {'error': f'unknown memory action: {action}'})
            elif section == 'status':
                import tools.system as sys_tools
                result = sys_tools.TOOLS['status']['handler']({})
                self._send_json(200, result)
            elif section == 'context':
                self._send_json(200, {'name': 'mirage-mcp-v2', 'status': 'ok', 'port': PORT_NEW})
            elif section == 'url_queue':
                self._send_json(200, {'cleared': 0, 'note': 'v2 has no url_queue'})
            else:
                self._send_json(404, {'error': f'unknown section: {section}'})
        except Exception as e:
            log.error(f'API route error {path}: {e}')
            self._send_json(500, {'error': str(e)})

'''

# Insert before _proxy_api_get
code = code.replace('    def _proxy_api_get(self, path: str):', api_handler + '    def _proxy_api_get(self, path: str):')

# Update do_GET to use _handle_api_route for /api/ paths in V2 directly
old = "        elif self.path.startswith('/api/'):\n            # Forward /api/* to legacy server (has memory/bootstrap etc)\n            self._proxy_api_get(self.path)"
new = "        elif self.path.startswith('/api/'):\n            # Handle /api/* directly in V2\n            self._handle_api_route(self.path)"
code = code.replace(old, new)

with open(v2_path, 'w', encoding='utf-8') as f:
    f.write(code)

print('OK: _handle_api_route added to V2 server')
print('_handle_api_route count:', code.count('_handle_api_route'))
