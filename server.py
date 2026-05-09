"""
server.py - MCPプロトコル薄い層
=================================
ここはプロトコルの橋渡しだけ。ビジネスロジックは各tools/*.pyに。
未実装のツールはfallback.pyで旧サーバーに転送。
"""
import json
import logging
import threading
import logging.handlers
import os
import sys
import time
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    # Windows での dual-bind 防止: allow_reuse_address=True (デフォルト) は
    # 「他プロセスがポート横取り bind 可能」という危険動作。SO_EXCLUSIVEADDRUSE で独占。
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# .env loader (shared with mcp-server)
# ---------------------------------------------------------------------------
def _load_env():
    env_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'mcp-server', '.env'))
    if not os.path.exists(env_path):
        return
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
_load_env()

from config import PORT_NEW
from fallback import call_fallback
import tools.memory as memory_tools

SERVER_STARTED_AT = time.time()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            os.path.join(os.path.dirname(__file__), 'logs', 'server.log'),
            maxBytes=5*1024*1024, backupCount=3, encoding='utf-8',
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
# P4-2 stage 3 precursor: optional dispatcher-backed task/loop.
# Set V2_USE_DISPATCHER=1 in the env to route run_task/run_loop through
# dispatcher.py instead of the legacy subprocess+thread path. Default off.
if os.environ.get('V2_USE_DISPATCHER', '').strip() == '1':
    import tools.task_v2 as task_tools;     TOOLS.update(task_tools.TOOLS)
    import tools.loop_v2 as loop_tools;     TOOLS.update(loop_tools.TOOLS)
    log.info('v2 dispatcher path enabled (V2_USE_DISPATCHER=1)')
else:
    import tools.task     as task_tools;     TOOLS.update(task_tools.TOOLS)
    import tools.loop     as loop_tools;     TOOLS.update(loop_tools.TOOLS)
import tools.pipeline as pipeline_tools; TOOLS.update(pipeline_tools.TOOLS)
import tools.vision   as vision_tools;   TOOLS.update(vision_tools.TOOLS)
import tools.windows_ops as winops_tools; TOOLS.update(winops_tools.TOOLS)
import tools.ai       as ai_tools;        TOOLS.update(ai_tools.TOOLS)
import tools.step6    as step6_tools;     TOOLS.update(step6_tools.TOOLS)

log.info(f'Registered tools: {list(TOOLS.keys())}')


def _file_age_sec(path: str):
    try:
        return round(max(0.0, time.time() - os.path.getmtime(path)), 3)
    except Exception:
        return None


def _health_payload(deep: bool = False) -> dict:
    """Return operational health without mutating memory state."""
    warnings = []
    payload = {
        'name': 'mirage-mcp-v2',
        'version': '1.0.0',
        'port': PORT_NEW,
        'tools': len(TOOLS),
        'status': 'ok',
        'pid': os.getpid(),
        'uptime_sec': round(time.time() - SERVER_STARTED_AT, 3),
        'heartbeat_age_sec': _file_age_sec(HEARTBEAT_FILE),
        'pid_file': PID_FILE,
        'heartbeat_file': HEARTBEAT_FILE,
        'warnings': warnings,
    }
    if not deep:
        return payload

    memory_db_ok = False
    semantic_lite_ok = False
    maintenance_recommended_count = None
    try:
        import sqlite3
        from memory import store as mem_store
        db_path = getattr(mem_store, 'DB_PATH', r'C:\MirageWork\mcp-server\data\memory.db')
        con = sqlite3.connect(db_path, timeout=2)
        try:
            con.execute('SELECT 1').fetchone()
            memory_db_ok = True
        finally:
            con.close()
    except Exception as e:
        warnings.append(f'memory_db: {e}')

    try:
        from memory import store as mem_store
        sem = mem_store.semantic_lite_status('mirage-infra')
        semantic_lite_ok = bool(sem.get('exists')) and not bool(sem.get('error'))
    except Exception as e:
        warnings.append(f'semantic_lite: {e}')

    try:
        mon = TOOLS['memory_maintenance_monitor']['handler']({
            'dry_run': True,
            'allow_auto': False,
            'max_runtime_sec': 10,
        })
        maintenance_recommended_count = int(mon.get('recommended_count') or 0)
    except Exception as e:
        warnings.append(f'maintenance_monitor: {e}')

    payload.update({
        'status': 'ok' if memory_db_ok else 'degraded',
        'memory_db_ok': memory_db_ok,
        'semantic_lite_ok': semantic_lite_ok,
        'maintenance_recommended_count': maintenance_recommended_count,
        'warnings': warnings,
    })
    return payload

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
    """MCP仕様: tools/callの結果はcontent配列でラップする"""
    name = params.get('name', '')
    args = params.get('arguments', {})

    def wrap_result(data: dict, is_error: bool = False) -> dict:
        """MCPプロトコル準拠: content配列にラップ"""
        resp = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
        if is_error:
            resp['isError'] = True
        return resp

    if name in TOOLS:
        log.info(f'tool call (v2): {name}')
        try:
            result = TOOLS[name]['handler'](args)
            return wrap_result(result, is_error='error' in result)
        except Exception as e:
            log.error(f'tool {name} error: {e}')
            return wrap_result({'error': str(e)}, is_error=True)
    else:
        # 未実装 → 旧サーバーにフォールバック
        log.info(f'tool call (fallback): {name}')
        fb_result = call_fallback(name, args)
        return wrap_result(fb_result, is_error='error' in fb_result)


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
            self._send_json(200, _health_payload())
        elif self.path == '/health/deep':
            self._send_json(200, _health_payload(deep=True))
        elif self.path == '/mcp':
            # SSE初期化用
            self._send_json(200, {'jsonrpc': '2.0', 'result': {}})
        elif self.path.startswith('/api/'):
            # Handle /api/* directly in V2
            self._handle_api_route(self.path)
        else:
            self._send_json(404, {'error': 'not found'})



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
                    args = {'namespace': ns}
                    if 'max_chars' in qs:
                        args['max_chars'] = qs.get('max_chars', ['800'])[0]
                    if 'query' in qs:
                        args['query'] = qs.get('query', [''])[0]
                    if 'top_n' in qs:
                        args['top_n'] = qs.get('top_n', ['0'])[0]
                    result = mem_tools.TOOLS['memory_bootstrap']['handler'](args)
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
            elif section == 'ai':
                # /api/v1/ai/context : Self-Describing System Interface.
                # Pull-only manual that any AI client (Code, GPT, etc.) can
                # fetch to align mental model with reality. facts only.
                if action == 'context':
                    import tools.ai as ai_tools
                    result = ai_tools.TOOLS['ai_context']['handler']({})
                    self._send_json(200, result)
                else:
                    self._send_json(404, {'error': f'unknown ai action: {action}'})
            elif section == 'url_queue':
                self._send_json(200, {'cleared': 0, 'note': 'v2 has no url_queue'})
            elif section == 'adb':
                import tools.device as dev_tools
                if action == 'devices':
                    result = dev_tools.TOOLS['adb_devices']['handler']({})
                    self._send_json(200, result)
                else:
                    cmd = qs.get('cmd', [''])[0]
                    device = qs.get('device', [''])[0]
                    result = dev_tools.TOOLS['adb_shell']['handler']({'command': cmd, 'device': device})
                    self._send_json(200, result)
            elif section == 'git':
                import tools.system as sys_tools2
                result = sys_tools2.TOOLS['git_status']['handler']({})
                self._send_json(200, result)
            elif section == 'exec':
                import urllib.parse as _up
                cmd = _up.unquote_plus(qs.get('cmd', [''])[0])
                import tools.system as sys_tools3
                result = sys_tools3.TOOLS['run_command']['handler']({'command': cmd})
                self._send_json(200, result)
            elif section == 'read':
                file_path = qs.get('path', [''])[0]
                import tools.system as sys_tools4
                result = sys_tools4.TOOLS['read_file']['handler']({'path': file_path})
                self._send_json(200, result)
            elif section == 'list':
                file_path = qs.get('path', [''])[0]
                import tools.system as sys_tools5
                result = sys_tools5.TOOLS['list_files']['handler']({'path': file_path})
                self._send_json(200, result)
            else:
                self._send_json(404, {'error': f'unknown section: {section}'})
        except Exception as e:
            log.error(f'API route error {path}: {e}')
            self._send_json(500, {'error': str(e)})

    def _handle_rest_post(self, path: str, body: bytes):
        """Dispatch REST POST to the appropriate tools.memory handler.
        Accepts params from both the query string and the JSON body;
        body values override query values on collision."""
        import urllib.parse
        import json as _json
        try:
            parsed = urllib.parse.urlparse(path)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            seg = parsed.path.lstrip("/").split("/")  # [api, v1, memory, compact]
            if len(seg) < 4 or seg[0] != "api" or seg[1] != "v1":
                self._send_json(404, {"error": f"unknown REST path: {parsed.path}"})
                return
            section = seg[2]
            action  = seg[3]
            # Parse JSON body (best-effort; empty body is fine)
            body_args = {}
            if body:
                try:
                    body_args = _json.loads(body)
                except Exception:
                    body_args = {}
            args = {**qs, **body_args}  # body wins
            # Only memory section is currently REST-POST enabled.
            # Extend here (tools.system, tools.device, etc.) if needed.
            if section == "memory":
                import tools.memory as mem_tools
                if action == "decision":
                    action = "append_decision"
                tool_name = "memory_" + action
                tool = mem_tools.TOOLS.get(tool_name)
                if not tool:
                    self._send_json(404, {"error": f"unknown memory action: {action}"})
                    return
                # Coerce numeric query-string params that arrive as strings.
                # tools handlers expect int for these, but parse_qs gives str.
                schema_props = (tool.get("schema") or {}).get("properties", {})
                for k, spec in schema_props.items():
                    if k in args and spec.get("type") == "integer" and isinstance(args[k], str):
                        try:
                            args[k] = int(args[k])
                        except ValueError:
                            pass
                result = tool["handler"](args)
                self._send_json(200, result)
                return
            self._send_json(404, {"error": f"REST POST not implemented for section: {section}"})
        except Exception as e:
            log.error(f"REST POST error {path}: {e}")
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        # REST POST routing (P0-2.5 aftermath fix, 2026-04-18):
        # v2 server was JSON-RPC only; REST paths like /api/v1/memory/compact
        # fell through to the JSON-RPC dispatcher which returned an empty
        # "unknown method" error. Now we pre-check for /api/v1/ REST paths
        # and route them to the tools.memory.TOOLS handlers directly.
        if self.path.startswith("/api/v1/"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
            self._handle_rest_post(self.path, body)
            return

        # Handle /restart endpoint
        if self.path == '/restart':
            self._send_json(200, {'status': 'restarting', 'pid': os.getpid()})
            log.info('Restart requested via /restart endpoint')
            def _do_restart():
                time.sleep(0.3)
                # Delete PID/heartbeat and exit - scheduled task will restart
                release_lock()
                try:
                    os.remove(HEARTBEAT_FILE)
                except OSError:
                    pass
                log.info('Exiting for restart (scheduled task will restart)...')
                os._exit(0)
            threading.Thread(target=_do_restart, daemon=True).start()
            return

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
# PIDファイルによる排他制御
# ---------------------------------------------------------------------------

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

PID_FILE = os.path.join(os.path.dirname(__file__), 'server.pid')

def check_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is running (Windows compatible).
    Uses exact PID match to avoid false positives from other python processes.
    """
    import subprocess
    try:
        result = subprocess.run(
            ['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'],
            capture_output=True, text=True, timeout=5
        )
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
        return False

def acquire_lock() -> bool:
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
    return True

def release_lock():
    """Release PID lock"""
    try:
        os.remove(PID_FILE)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    os.makedirs(os.path.join(os.path.dirname(__file__), 'logs'), exist_ok=True)
    
    if not acquire_lock():
        log.warning(f'Another instance is already running. Exiting.')
        sys.exit(0)
    
    try:
        hb = threading.Thread(target=_heartbeat_loop, daemon=True)
        hb.start()
        server = ExclusiveThreadingHTTPServer(('0.0.0.0', PORT_NEW), MCPHandler)
        log.info(f'mcp-server-v2 listening on port {PORT_NEW}')
        log.info(f'Fallback: http://localhost:3000')
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('Shutdown')
    except Exception as e:
        log.error(f'Server error: {e}')
    finally:
        release_lock()
        try:
            os.remove(HEARTBEAT_FILE)
        except OSError:
            pass
        try:
            server.server_close()
        except:
            pass
