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

# [2026-05-11 da06ca55] Tried eager-import google.generativeai here to fix the
# handler-thread import hang. It made V2 STARTUP itself hang (no "Registered
# tools" log line within 5+ min). So the import is genuinely slow on this V2
# main-thread context too — not a thread-locality issue. Action A confirmed
# negative; next try Action B (subprocess-isolated Gemini wrapper) per da06ca55.
# Reverted; do NOT re-add this without subprocess isolation.

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
import tools.pipeline as pipeline_tools; TOOLS.update(pipeline_tools.TOOLS)  # no-op stubs (a4a530a successor)
import tools.vision   as vision_tools;   TOOLS.update(vision_tools.TOOLS)
import tools.windows_ops as winops_tools; TOOLS.update(winops_tools.TOOLS)
import tools.ai       as ai_tools;        TOOLS.update(ai_tools.TOOLS)
import tools.step6    as step6_tools;     TOOLS.update(step6_tools.TOOLS)

log.info(f'Registered tools: {list(TOOLS.keys())}')


# ---------------------------------------------------------------------------
# Idempotency + chunk upload state (for /api/v1/memory GET write endpoints)
# ---------------------------------------------------------------------------
# Idempotency: persistent table in memory.db, INSERT-time lazy cleanup of >24h.
# Chunk upload: volatile in-process dict, 60-min TTL, cleared on restart.
_IDEM_TTL_SEC   = 24 * 3600
_CHUNK_TTL_SEC  = 60 * 60
_CHUNK_MAX_DATA = 32 * 1024          # base64-decoded bytes per chunk
_CHUNK_MAX_TOTAL = 1024 * 1024       # base64-decoded bytes per txn total
_CONTENT_MAX = 64 * 1024              # bytes per content field on single-shot append
_CHUNK_STATE: dict = {}              # txn_id -> {'created_at', 'ns', 'type', 'imp', 'summary', 'parts': {seq: b64str}}
_CHUNK_LOCK = threading.Lock()

def _idem_db_init():
    """Ensure idempotency_keys table exists. Safe to call multiple times."""
    try:
        import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
        from memory_store import _connect
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS idempotency_keys ("
                "idem TEXT PRIMARY KEY, entry_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        log.warning(f'idempotency table init skipped: {e}')

def _idem_lookup(idem: str):
    """Return existing entry_id for a non-expired idem, or None."""
    if not idem:
        return None
    try:
        import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
        from memory_store import _connect
        con = _connect()
        try:
            row = con.execute(
                "SELECT entry_id FROM idempotency_keys WHERE idem=? "
                "AND created_at >= strftime('%s','now') - ?",
                (idem, _IDEM_TTL_SEC)
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except Exception as e:
        log.warning(f'idem_lookup failed: {e}')
        return None

def _idem_record(idem: str, entry_id: str):
    """Record idem→entry_id, lazy-deleting >24h expired rows in same call."""
    if not idem or not entry_id:
        return
    try:
        import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
        from memory_store import _connect
        con = _connect()
        try:
            con.execute("DELETE FROM idempotency_keys WHERE created_at < strftime('%s','now') - ?", (_IDEM_TTL_SEC,))
            con.execute(
                "INSERT OR IGNORE INTO idempotency_keys (idem, entry_id, created_at) "
                "VALUES (?, ?, strftime('%s','now'))",
                (idem, entry_id)
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        log.warning(f'idem_record failed: {e}')

_idem_db_init()


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


def _query_bool(qs: dict, name: str, default: bool) -> bool:
    raw = (qs.get(name) or [None])[0]
    if raw is None:
        return default
    return str(raw).strip().lower() not in ('0', 'false', 'no', 'off')

# ---------------------------------------------------------------------------
# MCP ハンドラ
# ---------------------------------------------------------------------------
def _tools_version() -> str:
    """Stable hash of the current tool registry (names + descriptions).
    Bumps whenever a tool is added/removed/renamed/redescribed. Clients that
    re-poll tools/list can detect drift via this field without diffing.
    """
    import hashlib
    payload = '\n'.join(
        f'{name}\t{spec.get("description", "")}'
        for name, spec in sorted(TOOLS.items())
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def handle_tools_list() -> dict:
    return {
        'tools': [
            {
                'name': name,
                'description': spec.get('description', ''),
                'inputSchema': spec.get('schema', {}),
            }
            for name, spec in TOOLS.items()
        ],
        '_tools_version': _tools_version(),
        '_tools_count': len(TOOLS),
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
        # Prevent Cloudflare edge from caching realtime API state (memory writes,
        # context, health). claude.ai web_fetch cannot use cache-buster URLs
        # because of its allowlist, so the API itself must opt out of caching.
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self) -> bool:
        """V2-level Bearer check (defense in depth alongside buffer_proxy).
        Disabled when MIRAGE_MCP_TOKEN env is unset. /health and /restart are
        exempt so loopback callers (V1 health-check, mcp_guard_v2 restart
        trigger) keep working.

        Accepts api_key=<token> query param as a fallback for clients that
        cannot set Authorization header (e.g. claude.ai web_fetch). Query-
        string credentials may be logged by upstream CDN/proxy; treat the
        token as comparable risk to a URL secret.
        """
        token = os.environ.get('MIRAGE_MCP_TOKEN', '')
        if not token:
            return True
        # path may include query string; strip it for the exempt check
        path = (self.path or '').split('?', 1)[0]
        if path in ('/', '/health', '/health/deep', '/health/report', '/restart'):
            return True
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer ') and auth[len('Bearer '):].strip() == token:
            return True
        # Query-param fallback for browser-side fetch clients
        try:
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlparse(self.path or '').query)
            return qs.get('api_key', [''])[0] == token
        except Exception:
            return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if not self._auth_ok():
            self._send_json(401, {'error': 'unauthorized',
                                   'hint': 'set Authorization: Bearer <MIRAGE_MCP_TOKEN>'})
            return
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        if path in ('/', '/health'):
            self._send_json(200, _health_payload())
        elif path == '/health/deep':
            self._send_json(200, _health_payload(deep=True))
        elif path == '/health/report':
            self._send_json(200, TOOLS['mcp_health_report']['handler']({
                'include_git': _query_bool(qs, 'include_git', True),
                'include_deep': _query_bool(qs, 'include_deep', True),
            }))
        elif path == '/mcp':
            # SSE初期化用
            self._send_json(200, {'jsonrpc': '2.0', 'result': {}})
        elif path.startswith('/api/'):
            # Handle /api/* directly in V2
            self._handle_api_route(self.path)
        else:
            self._send_json(404, {'error': 'not found'})



    def _build_context_response(self, qs: dict) -> dict:
        """Build /api/v1/context v2.1 response.

        Additive over the original 3-key shape (name/status/port preserved
        verbatim). Adds URL templates so browser-side AI clients can call
        memory APIs by URL alone without setting Bearer headers.
        """
        import datetime as _dt
        import shutil as _shutil
        # Caller-supplied api_key roundtripped into template URLs so the
        # caller can use the returned URLs directly. If absent we leave
        # literal <KEY> for manual substitution.
        caller_key = qs.get('api_key', ['<KEY>'])[0] or '<KEY>'
        ns = qs.get('namespace', [''])[0] or None
        BASE = 'https://mcp.mirage-sys.com/api/v1'

        def _tpl(path_with_qs: str) -> str:
            return f'{BASE}/{path_with_qs}&api_key={caller_key}'

        out = {
            # --- existing keys preserved verbatim (additive guarantee) ---
            'name': 'mirage-mcp-v2',
            'status': 'ok',
            'port': PORT_NEW,
            # --- v2.1 additions ---
            'version': '2.1.0',
            'namespace': ns,
            'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat(),
            'apis': {
                'memory': {
                    'append_decision_template': _tpl('memory/append_decision?namespace={ns}&importance={imp}&summary={s}&content={c}&idem={idem}'),
                    'append_raw_template':      _tpl('memory/append_raw?namespace={ns}&content={c}&idem={idem}'),
                    'search_template':          _tpl('memory/search?namespace={ns}&q={q}'),
                    'get_template':             _tpl('memory/get?id={id}'),
                    'compact_template':         _tpl('memory/compact?namespace={ns}&model=llama3.2:3b&max_chars=800'),
                    'chunk_begin_template':     _tpl('memory/chunk/begin?ns={ns}&type={type}&imp={imp}&summary={s}'),
                    'chunk_append_template':    _tpl('memory/chunk/append?txn={txn}&seq={seq}&data={base64}'),
                    'chunk_commit_template':    _tpl('memory/chunk/commit?txn={txn}'),
                },
                'files': {
                    'read_template': _tpl('read?path={path}'),
                    'list_template': _tpl('list?path={path}'),
                },
                'exec': {
                    'command_template': _tpl('exec?cmd={cmd}'),
                },
            },
            'bootstrap_urls': [
                _tpl(f'memory/bootstrap?namespace={n}') for n in
                ('mirage-vulkan', 'mirage-android', 'mirage-infra', 'mirage-design', 'mirage-general')
            ],
            'url_queue': [],
            'namespaces_available': ['mirage-vulkan', 'mirage-android', 'mirage-infra', 'mirage-design', 'mirage-general'],
        }

        # recent_activity (best effort; never block the response)
        recent = {'last_decision_ts': None, 'decisions_today': 0, 'active_pipelines': []}
        try:
            import sys as _sys; _sys.path.insert(0, r'C:\MirageWork\mirage-shared')
            from memory_store import _connect as _mc
            _con = _mc()
            try:
                row = _con.execute(
                    "SELECT MAX(created_at) FROM entries WHERE type='decision'"
                ).fetchone()
                if row and row[0]:
                    recent['last_decision_ts'] = _dt.datetime.fromtimestamp(int(row[0]), _dt.timezone.utc).isoformat()
                row2 = _con.execute(
                    "SELECT COUNT(*) FROM entries WHERE type='decision' "
                    "AND created_at >= strftime('%s','now','start of day')"
                ).fetchone()
                recent['decisions_today'] = int(row2[0]) if row2 else 0
            finally:
                _con.close()
        except Exception:
            pass
        out['recent_activity'] = recent

        # system_health (best effort)
        health = {'memory_db_size_mb': 0.0, 'fastembed_status': 'unknown', 'disk_free_gb': 0.0}
        try:
            db_path = r'C:\MirageWork\mcp-server\data\memory.db'
            if os.path.exists(db_path):
                health['memory_db_size_mb'] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
        except Exception:
            pass
        try:
            health['disk_free_gb'] = round(_shutil.disk_usage('C:\\').free / (1024 ** 3), 2)
        except Exception:
            pass
        # Fast non-blocking check: existence + mtime of the fastembed npz
        # (avoid cold-import probe which can hang 30-90s+ under commit pressure;
        # see decision 22254fc7 for the 2917s empirical measurement)
        try:
            fe_npz = r'C:\MirageWork\mcp-server\data\memory_fastembed.npz'
            if not os.path.exists(fe_npz):
                health['fastembed_status'] = 'unavailable'
            else:
                age_hr = (time.time() - os.path.getmtime(fe_npz)) / 3600.0
                health['fastembed_status'] = 'degraded' if age_hr > 48 else 'ok'
        except Exception:
            health['fastembed_status'] = 'unknown'
        out['system_health'] = health

        return out

    def _handle_chunk(self, sub: str, qs: dict, ns: str):
        """Chunk upload for content > 64KB. Volatile, 60min TTL."""
        import base64 as _b64
        import uuid as _uuid
        import datetime as _dt3
        _NS_OK = ('mirage-vulkan', 'mirage-android', 'mirage-infra', 'mirage-design', 'mirage-general')

        if sub == 'begin':
            # chunk APIs use `ns` (short form per spec); fall back to `namespace`
            ns = qs.get('ns', [None])[0] or qs.get('namespace', [ns])[0] or ns
            if ns not in _NS_OK:
                self._send_json(400, {'error': f'unknown namespace: {ns}'})
                return
            etype = qs.get('type', ['decision'])[0]
            if etype not in ('decision', 'raw'):
                self._send_json(400, {'error': 'type must be decision or raw'})
                return
            try:
                imp_val = int(qs.get('imp', ['3'])[0])
            except (ValueError, TypeError):
                imp_val = 3
            txn = _uuid.uuid4().hex
            with _CHUNK_LOCK:
                # Lazy expire stale txns
                _now = time.time()
                for k in list(_CHUNK_STATE.keys()):
                    if _now - _CHUNK_STATE[k]['created_at'] > _CHUNK_TTL_SEC:
                        _CHUNK_STATE.pop(k, None)
                _CHUNK_STATE[txn] = {
                    'created_at': _now, 'ns': ns, 'type': etype, 'imp': imp_val,
                    'summary': qs.get('summary', [''])[0], 'parts': {},
                    'total_bytes': 0,
                }
            self._send_json(200, {'ok': True, 'txn': txn, 'ttl_sec': _CHUNK_TTL_SEC})
            return

        if sub in ('append', 'commit'):
            txn = qs.get('txn', [''])[0]
            if not txn:
                self._send_json(400, {'error': 'txn required'})
                return
            with _CHUNK_LOCK:
                state = _CHUNK_STATE.get(txn)
                if not state:
                    self._send_json(404, {'error': 'unknown txn (may have expired)'})
                    return
                if time.time() - state['created_at'] > _CHUNK_TTL_SEC:
                    _CHUNK_STATE.pop(txn, None)
                    self._send_json(410, {'error': 'txn expired'})
                    return

            if sub == 'append':
                try:
                    seq = int(qs.get('seq', ['-1'])[0])
                except (ValueError, TypeError):
                    seq = -1
                if seq < 0:
                    self._send_json(400, {'error': 'seq required (non-negative integer)'})
                    return
                data_b64 = qs.get('data', [''])[0]
                if not data_b64:
                    self._send_json(400, {'error': 'data (base64) required'})
                    return
                try:
                    decoded = _b64.b64decode(data_b64, validate=True)
                except Exception:
                    self._send_json(400, {'error': 'data is not valid base64'})
                    return
                if len(decoded) > _CHUNK_MAX_DATA:
                    self._send_json(413, {'error': f'chunk exceeds {_CHUNK_MAX_DATA // 1024}KB per part'})
                    return
                with _CHUNK_LOCK:
                    state = _CHUNK_STATE.get(txn)
                    if not state:
                        self._send_json(404, {'error': 'unknown txn'})
                        return
                    if state['total_bytes'] + len(decoded) > _CHUNK_MAX_TOTAL:
                        self._send_json(413, {'error': f'txn total exceeds {_CHUNK_MAX_TOTAL // 1024}KB'})
                        return
                    state['parts'][seq] = data_b64
                    state['total_bytes'] += len(decoded)
                self._send_json(200, {'ok': True, 'txn': txn, 'seq': seq, 'total_bytes': state['total_bytes']})
                return

            if sub == 'commit':
                with _CHUNK_LOCK:
                    state = _CHUNK_STATE.pop(txn, None)
                if not state:
                    self._send_json(404, {'error': 'unknown txn'})
                    return
                # Reassemble in seq order, decode, concat
                try:
                    parts_sorted = sorted(state['parts'].items(), key=lambda kv: kv[0])
                    full = b''.join(_b64.b64decode(p[1]) for p in parts_sorted).decode('utf-8', errors='replace')
                except Exception as e:
                    self._send_json(500, {'error': f'reassembly failed: {e}'})
                    return
                # Dispatch to internal tool
                import tools.memory as mem_tools
                tool_name = 'memory_append_' + state['type']
                args = {'namespace': state['ns'], 'content': full}
                if state['type'] == 'decision':
                    args['title'] = state['summary']
                    args['importance'] = state['imp']
                result = mem_tools.TOOLS[tool_name]['handler'](args)
                entry_id = (result or {}).get('id') if isinstance(result, dict) else None
                self._send_json(200, {
                    'ok': bool(entry_id),
                    'entry_id': entry_id,
                    'namespace': state['ns'],
                    'importance': state['imp'] if state['type'] == 'decision' else None,
                    'ts': _dt3.datetime.now(_dt3.timezone.utc).isoformat(),
                    'parts_count': len(state['parts']),
                    'total_bytes': state['total_bytes'],
                    'warnings': (result or {}).get('warnings', []) if isinstance(result, dict) else []
                })
                return

        self._send_json(404, {'error': f'unknown chunk action: {sub}'})

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
                # Sub-path support for /api/v1/memory/{action}/{sub} (chunk APIs)
                sub = seg[4] if len(seg) > 4 else ''
                _NS_OK = ('mirage-vulkan', 'mirage-android', 'mirage-infra', 'mirage-design', 'mirage-general')

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
                    q = qs.get('q', [''])[0] or qs.get('query', [''])[0]
                    result = mem_tools.TOOLS['memory_search']['handler']({'namespace': ns, 'query': q})
                    self._send_json(200, result)
                elif action == 'get':
                    id_val = qs.get('id', [''])[0]
                    if not id_val:
                        self._send_json(400, {'error': 'id required'})
                    else:
                        result = mem_tools.TOOLS['memory_get']['handler']({'id': id_val})
                        self._send_json(200, result)
                elif action in ('append_decision', 'append_raw'):
                    # Phase 1-4: namespace validation
                    if ns not in _NS_OK:
                        self._send_json(400, {'error': f'unknown namespace: {ns}', 'allowed': list(_NS_OK)})
                        return
                    content = qs.get('content', [''])[0]
                    if not content:
                        self._send_json(400, {'error': 'content required'})
                        return
                    # Phase 1-4: 64KB content limit (single-shot path)
                    if len(content.encode('utf-8')) > _CONTENT_MAX:
                        self._send_json(413, {
                            'error': f'content exceeds {_CONTENT_MAX // 1024}KB single-shot limit',
                            'hint': 'use chunk APIs (memory/chunk/begin → append → commit)'
                        })
                        return
                    # Phase 1-3: idempotency check
                    idem = qs.get('idem', [''])[0] or None
                    if idem:
                        existing = _idem_lookup(idem)
                        if existing:
                            self._send_json(200, {
                                'ok': True, 'entry_id': existing, 'namespace': ns,
                                'idempotent_replay': True, 'warnings': []
                            })
                            return
                    # Build args + dispatch
                    args = {'namespace': ns, 'content': content}
                    try:
                        imp_val = int(qs.get('importance', ['3'])[0])
                    except (ValueError, TypeError):
                        imp_val = 3
                    if action == 'append_decision':
                        args['title']      = qs.get('summary', [''])[0] or qs.get('title', [''])[0]
                        args['importance'] = imp_val
                    result = mem_tools.TOOLS['memory_' + action]['handler'](args)
                    entry_id = (result or {}).get('id') if isinstance(result, dict) else None
                    if idem and entry_id:
                        _idem_record(idem, entry_id)
                    import datetime as _dt2
                    self._send_json(200, {
                        'ok': bool(entry_id),
                        'entry_id': entry_id,
                        'namespace': ns,
                        'importance': imp_val if action == 'append_decision' else None,
                        'ts': _dt2.datetime.now(_dt2.timezone.utc).isoformat(),
                        'warnings': (result or {}).get('warnings', []) if isinstance(result, dict) else []
                    })
                elif action == 'chunk':
                    self._handle_chunk(sub, qs, ns)
                else:
                    self._send_json(404, {'error': f'unknown memory action: {action}'})
            elif section == 'status':
                import tools.system as sys_tools
                result = sys_tools.TOOLS['status']['handler']({})
                self._send_json(200, result)
            elif section == 'context':
                self._send_json(200, self._build_context_response(qs))
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
        if not self._auth_ok():
            self._send_json(401, {'error': 'unauthorized',
                                   'hint': 'set Authorization: Bearer <MIRAGE_MCP_TOKEN>'})
            return
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
        # bind 127.0.0.1 only: V2 is reached via V1 (also localhost) and
        # buffer_proxy (also localhost). LAN exposure is unintended attack
        # surface (no auth in V2 tools dispatch).
        server = ExclusiveThreadingHTTPServer(('127.0.0.1', PORT_NEW), MCPHandler)
        log.info(f'mcp-server-v2 listening on 127.0.0.1:{PORT_NEW} (loopback only)')
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
