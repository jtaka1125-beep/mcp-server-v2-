"""
server.py - MCPプロトコル薄い層
=================================
ここはプロトコルの橋渡しだけ。ビジネスロジックは各tools/*.pyに。
未実装のツールはfallback.pyで旧サーバーに転送。
"""
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from config import PORT_NEW
from fallback import call_fallback
import tools.memory as memory_tools

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), 'logs', 'server.log'),
            encoding='utf-8',
        ),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ツール登録: ここに追加していく
# ---------------------------------------------------------------------------
TOOLS: dict = {}
TOOLS.update(memory_tools.TOOLS)
import tools.system as system_tools; TOOLS.update(system_tools.TOOLS)
import tools.device as device_tools; TOOLS.update(device_tools.TOOLS)
import tools.build    as build_tools;    TOOLS.update(build_tools.TOOLS)
import tools.task     as task_tools;     TOOLS.update(task_tools.TOOLS)
import tools.loop     as loop_tools;     TOOLS.update(loop_tools.TOOLS)
import tools.pipeline as pipeline_tools; TOOLS.update(pipeline_tools.TOOLS)
import tools.vision   as vision_tools;   TOOLS.update(vision_tools.TOOLS)
# 今後追加:
# from tools.device import TOOLS as device_tools; TOOLS.update(device_tools)
# from tools.pipeline import TOOLS as pipeline_tools; TOOLS.update(pipeline_tools)

log.info(f'Registered tools: {list(TOOLS.keys())}')

# ---------------------------------------------------------------------------
# MCP ハンドラ
# ---------------------------------------------------------------------------
def handle_tools_list() -> dict:
    return {
        'tools': [
            {
                'name': name,
                'description': spec.get('description', ''),
                'inputSchema': spec.get('schema', {}),
            }
            for name, spec in TOOLS.items()
        ]
    }


def handle_tools_call(params: dict) -> Any:
    name = params.get('name', '')
    args = params.get('arguments', {})

    if name in TOOLS:
        log.info(f'tool call (v2): {name}')
        try:
            return TOOLS[name]['handler'](args)
        except Exception as e:
            log.error(f'tool {name} error: {e}')
            return {'error': str(e)}
    else:
        # 未実装 → 旧サーバーにフォールバック
        log.info(f'tool call (fallback): {name}')
        return call_fallback(name, args)


# ---------------------------------------------------------------------------
# HTTP サーバー (Streamable HTTP / JSON-RPC)
# ---------------------------------------------------------------------------
class MCPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(f'HTTP {self.address_string()} {fmt % args}')

    def _send_json(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path in ('/', '/health'):
            self._send_json(200, {
                'name': 'mirage-mcp-v2',
                'version': '1.0.0',
                'port': PORT_NEW,
                'tools': len(TOOLS),
                'status': 'ok',
            })
        elif self.path == '/mcp':
            # SSE初期化用
            self._send_json(200, {'jsonrpc': '2.0', 'result': {}})
        else:
            self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)

        try:
            req = json.loads(body)
        except Exception:
            self._send_json(400, {'error': 'invalid JSON'})
            return

        method = req.get('method', '')
        params = req.get('params', {})
        req_id = req.get('id', 1)

        try:
            if method == 'initialize':
                result = {
                    'protocolVersion': '2025-03-26',
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'mirage-mcp-v2', 'version': '1.0.0'},
                }
            elif method == 'tools/list':
                result = handle_tools_list()
            elif method == 'tools/call':
                result = handle_tools_call(params)
            elif method == 'notifications/initialized':
                result = {}
            else:
                result = {'error': f'unknown method: {method}'}

            self._send_json(200, {
                'jsonrpc': '2.0',
                'id': req_id,
                'result': result,
            })

        except Exception as e:
            log.error(f'handler error method={method}: {e}')
            self._send_json(500, {
                'jsonrpc': '2.0',
                'id': req_id,
                'error': {'code': -32000, 'message': str(e)},
            })


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    os.makedirs(os.path.join(os.path.dirname(__file__), 'logs'), exist_ok=True)
    server = HTTPServer(('0.0.0.0', PORT_NEW), MCPHandler)
    log.info(f'mcp-server-v2 listening on port {PORT_NEW}')
    log.info(f'Fallback: http://localhost:3000')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('Shutdown')
        server.server_close()
