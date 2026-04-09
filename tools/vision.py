"""
tools/vision.py - Vision・AI系ツール
=======================================
detect_popup, ai_analyze, macro_screenshot, chat_with_ai
LLM呼び出しはllm.call()経由。
"""
import os
import sys
import base64
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, r'C:\MirageWork\mcp-server')

import llm

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# detect_popup
# ---------------------------------------------------------------------------
def tool_detect_popup(args: dict) -> dict:
    device         = (args or {}).get('device', '')
    image_path     = (args or {}).get('image_path', '')
    auto_register  = (args or {}).get('auto_register', False)
    timeout        = int((args or {}).get('timeout', 120) or 120)
    use_mirage_frame = (args or {}).get('use_mirage_frame', False)

    # スクリーンショット取得
    if not image_path:
        from tools.device import tool_screenshot
        ss = tool_screenshot({'device': device})
        if 'error' in ss:
            return {'found': False, 'error': ss['error']}
        image_path = ss['path']

    # 画像をbase64に変換
    try:
        with open(image_path, 'rb') as f:
            image_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return {'found': False, 'error': f'image read failed: {e}'}

    prompt = """この画像はAndroidアプリの画面です。
広告ポップアップ・ダイアログ・閉じるボタンが存在する場合、その位置を返してください。

JSONのみ出力:
{
  "found": true/false,
  "element_type": "button/dialog/popup/unknown",
  "tap_x_percent": 0-100またはnull,
  "tap_y_percent": 0-100またはnull,
  "confidence": 0.0-1.0,
  "reasoning": "判断根拠"
}"""

    raw = llm.call(prompt, purpose='vision', max_tokens=300,
                   timeout=timeout, image_b64=image_b64)

    if not raw:
        return {'found': False, 'error': 'LLM returned empty response'}

    import json as _j
    try:
        si = raw.find('{')
        ei = raw.rfind('}') + 1
        result = _j.loads(raw[si:ei])
    except Exception:
        return {'found': False, 'error': f'JSON parse failed: {raw[:100]}'}

    # 座標変換（パーセント→ピクセル）
    if result.get('found') and result.get('tap_x_percent') is not None:
        # 解像度取得
        res_w, res_h = 1080, 1800  # デフォルト
        from config import DEVICES
        for d in DEVICES.values():
            if d.get('wifi', '') == device or not device:
                res_w = d.get('res_w', 1080)
                res_h = d.get('res_h', 1800)
                break
        result['tap_x'] = int(result['tap_x_percent'] / 100.0 * res_w)
        result['tap_y'] = int(result['tap_y_percent'] / 100.0 * res_h)

    result['image_path'] = image_path
    return result

# ---------------------------------------------------------------------------
# ai_analyze
# ---------------------------------------------------------------------------
def tool_ai_analyze(args: dict) -> dict:
    image_path = (args or {}).get('image_path', '')
    prompt     = (args or {}).get('prompt', 'この画面を分析してください。')
    timeout    = int((args or {}).get('timeout', 60) or 60)

    if not image_path:
        return {'error': 'image_path required'}

    try:
        with open(image_path, 'rb') as f:
            image_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return {'error': f'image read failed: {e}'}

    result = llm.call(prompt, purpose='vision', max_tokens=1000,
                      timeout=timeout, image_b64=image_b64)
    return {'result': result, 'image_path': image_path}

# ---------------------------------------------------------------------------
# macro_screenshot
# ---------------------------------------------------------------------------
def tool_macro_screenshot(args: dict) -> dict:
    """MacroAPI経由でスクリーンショット取得。既存サーバーのエンドポイントを流用。"""
    device_id = (args or {}).get('device_id', '')
    try:
        import requests
        resp = requests.get(
            f'http://localhost:3000/api/screenshot',
            params={'device': device_id},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        # フォールバック: ADB screencap
        from tools.device import tool_screenshot
        return tool_screenshot({'device': device_id})
    except Exception as e:
        from tools.device import tool_screenshot
        return tool_screenshot({'device': device_id})

# ---------------------------------------------------------------------------
# chat_with_ai
# ---------------------------------------------------------------------------
def tool_chat_with_ai(args: dict) -> dict:
    message = (args or {}).get('message', '')
    model   = (args or {}).get('model', 'groq')  # groq/gemini/claude
    if not message:
        return {'error': 'message required'}

    purpose_map = {
        'groq':   'general',
        'gemini': 'vision',
        'claude': 'code',
    }
    purpose = purpose_map.get(model, 'general')
    result  = llm.call(message, purpose=purpose, max_tokens=1000, timeout=30)
    return {'response': result, 'model': model}

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
TOOLS = {
    'detect_popup': {
        'description': 'Detect popup/dialog on Android screen using Gemini vision. Returns close button position.',
        'schema': {'type': 'object', 'properties': {
            'device':          {'type': 'string'},
            'image_path':      {'type': 'string'},
            'auto_register':   {'type': 'boolean'},
            'timeout':         {'type': 'integer'},
            'use_mirage_frame':{'type': 'boolean'},
        }},
        'handler': tool_detect_popup,
    },
    'ai_analyze': {
        'description': 'Analyze screen with Gemini vision model.',
        'schema': {'type': 'object', 'properties': {
            'image_path': {'type': 'string'},
            'prompt':     {'type': 'string'},
            'timeout':    {'type': 'integer'},
        }, 'required': ['image_path']},
        'handler': tool_ai_analyze,
    },
    'macro_screenshot': {
        'description': 'Capture device screen via MacroAPI.',
        'schema': {'type': 'object', 'properties': {
            'device_id': {'type': 'string'},
        }},
        'handler': tool_macro_screenshot,
    },
    'chat_with_ai': {
        'description': 'Send a message to Groq/Gemini/Claude and return the AI response.',
        'schema': {'type': 'object', 'properties': {
            'message': {'type': 'string'},
            'model':   {'type': 'string', 'enum': ['groq', 'gemini', 'claude']},
        }, 'required': ['message']},
        'handler': tool_chat_with_ai,
    },
}
