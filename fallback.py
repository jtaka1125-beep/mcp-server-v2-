"""
fallback.py - 未実装ツールを旧サーバー(port 3000)に転送
=========================================================
mcp-server-v2 で未実装のツールはここを通して旧サーバーに投げる。
"""
import requests
import json
import logging

log = logging.getLogger(__name__)
FALLBACK_URL = 'http://localhost:3000/mcp'

def call_fallback(tool_name: str, args: dict) -> dict:
    """旧サーバーのMCPエンドポイントにツール呼び出しを転送する。"""
    payload = {
        'jsonrpc': '2.0',
        'method': 'tools/call',
        'params': {'name': tool_name, 'arguments': args or {}},
        'id': 1,
    }
    try:
        resp = requests.post(
            FALLBACK_URL,
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=120,
        )
        resp.raise_for_status()
        rj = resp.json()
        if 'error' in rj:
            log.warning(f'fallback tool={tool_name} rpc_error={rj["error"]}')
            return {'error': rj['error'], '_fallback': True}
        result = rj.get('result', {})
        if isinstance(result, dict):
            result['_fallback'] = True
        return result
    except Exception as e:
        log.error(f'fallback failed tool={tool_name}: {e}')
        return {'error': str(e), '_fallback': True}
