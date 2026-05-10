import time
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
# classify_screen (llama-server E4B + mmproj direct, port 8091)
# ---------------------------------------------------------------------------
import urllib.request as _urlreq
import urllib.error as _urlerr
import json as _json

LLAMA_CLASSIFY_URL = os.environ.get(
    'LLAMA_CLASSIFY_URL', 'http://127.0.0.1:8091/v1/chat/completions'
)

CLASSIFY_SYSTEM_PROMPT = (
    'Classify Android screenshots. Output ONLY valid JSON: '
    '{"screen": "<class>", "confidence": <0.0-1.0>} '
    'Classes: ad (any advertisement/install prompt), login, feed, settings, home, dialog, video.'
)

# [P0 #2] Added deceptive-ad detection hint. Web-based interstitials with white
# background, text-heavy layout, and no explicit "AD" label are easy to confuse
# with feed/login. Anchor on structural ad markers (top-right close X + single
# dominant CTA) which are present even in minimal designs.
CLASSIFY_IMAGE_USER_PROMPT = (
    '"ad" includes app install ads and interstitial ads. '
    'IMPORTANT: even when the page looks minimal (white background, plain text, no "AD" label), '
    'classify as "ad" if BOTH conditions hold: (1) a small close button (X / × / Skip / 閉じる) '
    'in the top-right or top corner area, AND (2) a single large CTA button (Open / Install / 開く) '
    'dominating the bottom area. These are interstitial ad signatures. '
    'Classify this screenshot.'
)


# [P0 #1] Bottom-nav crop helper. The Android home indicator + 5-icon nav bar
# at the screen bottom is a strong "home screen" signal that biases E4B toward
# misclassifying full-screen interstitials. Cropping ~8% bottom removes the
# distractor without losing ad content (CTAs typically sit above the nav bar).
def _preprocess_image_for_classify(path: str, crop_bottom_pct: float = 0.08) -> str:
    """Return path to a preprocessed image; falls back to original on any failure.

    crop_bottom_pct: fraction of image height to crop from bottom (default 8%).
                     Set to 0 to disable.
    """
    if not crop_bottom_pct or crop_bottom_pct <= 0:
        return path
    try:
        from PIL import Image
        import tempfile as _tempfile
        with Image.open(path) as im:
            w, h = im.size
            crop_h = int(h * crop_bottom_pct)
            if crop_h <= 0 or crop_h >= h:
                return path
            cropped = im.crop((0, 0, w, h - crop_h))
            tmp = _tempfile.NamedTemporaryFile(suffix='_cls.png', delete=False)
            tmp.close()
            cropped.save(tmp.name, format='PNG')
            return tmp.name
    except Exception:
        return path


def _classify_call(messages: list, timeout: int) -> dict:
    payload = {
        'model': 'gemma4-e4b',
        'messages': messages,
        'max_tokens': 32,
        'temperature': 0.1,
    }
    req = _urlreq.Request(
        LLAMA_CLASSIFY_URL,
        data=_json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    t0 = time.monotonic()
    try:
        with _urlreq.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except (_urlerr.URLError, OSError) as e:
        return {
            '_error': f'llama-server unreachable: {e}',
            '_elapsed_ms': int((time.monotonic() - t0) * 1000),
        }
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    try:
        d = _json.loads(body)
        raw = d['choices'][0]['message']['content']
    except Exception as e:
        return {
            '_error': f'response parse failed: {e}',
            '_raw': body[:500].decode('utf-8', errors='replace'),
            '_elapsed_ms': elapsed_ms,
        }
    try:
        si = raw.find('{')
        ei = raw.rfind('}') + 1
        parsed = _json.loads(raw[si:ei])
    except Exception as e:
        return {'_error': f'classify JSON parse failed: {e}', '_raw': raw, '_elapsed_ms': elapsed_ms}
    return {'_parsed': parsed, '_raw': raw, '_elapsed_ms': elapsed_ms}


# ---------------------------------------------------------------------------
# dismiss_screen: Phase 2 b — E4B vision で close button 座標を返す
# ---------------------------------------------------------------------------
# layer2_cli.py (claude -p) の置換。Layer 2 は目、AX は手 (W15 PhaseB) の
# vision 部分を E4B に。AX verify は C++ 側で既存 verifyWithAx 流用。
# 同期先: dismiss_screen_client.cpp (C++ 側 prompt 複製)。

DISMISS_SYSTEM_PROMPT_TEMPLATE = (
    'Find the close button (X / × / Skip / 閉じる icon) in this Android ad screenshot. '
    'Output ONLY JSON: {{"x": int, "y": int, "confidence": float}}. '
    'If no close button is visible, output {{"x": null, "y": null, "confidence": 0.0}}. '
    'Image is {W}x{H} pixels. Close buttons are typically small (15-50px) in the top-right corner. '
    'Do NOT pick large install/CTA buttons (would trigger app install). '
    'Do NOT pick the home indicator at the bottom-edge of the screen.'
)
DISMISS_USER_PROMPT = 'Locate the close button. Return JSON only.'


def tool_dismiss_screen(args: dict) -> dict:
    args = args or {}
    image_path = args.get('image_path') or None
    timeout = int(args.get('timeout', 30) or 30)
    frame_w = int(args.get('frame_w', 1200) or 1200)
    frame_h = int(args.get('frame_h', 2000) or 2000)

    if not image_path:
        return {'error': 'image_path required'}

    try:
        with open(image_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return {'error': f'image read failed: {e}', 'details': {'image_path': image_path}}

    sys_prompt = DISMISS_SYSTEM_PROMPT_TEMPLATE.format(W=frame_w, H=frame_h)
    messages = [
        {'role': 'system', 'content': sys_prompt},
        {'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
            {'type': 'text', 'text': DISMISS_USER_PROMPT},
        ]},
    ]

    # llama-server 直叩き (classify_screen と同じ endpoint、prompt と max_tokens 違い)
    payload = {
        'model': 'gemma4-e4b',
        'messages': messages,
        'max_tokens': 64,        # 座標 + confidence で classify (32) より長め
        'temperature': 0.1,
    }
    req = _urlreq.Request(
        LLAMA_CLASSIFY_URL,
        data=_json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    t0 = time.monotonic()
    try:
        with _urlreq.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except (_urlerr.URLError, OSError) as e:
        return {'error': f'llama-server unreachable: {e}',
                'details': {'elapsed_ms': int((time.monotonic() - t0) * 1000)}}
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    try:
        d = _json.loads(body)
        raw = d['choices'][0]['message']['content']
    except Exception as e:
        return {'error': f'response parse failed: {e}',
                'details': {'raw': body[:500].decode('utf-8', errors='replace'),
                            'elapsed_ms': elapsed_ms}}

    # raw から JSON 抽出
    try:
        si = raw.find('{')
        ei = raw.rfind('}') + 1
        parsed = _json.loads(raw[si:ei])
    except Exception as e:
        return {'error': f'dismiss JSON parse failed: {e}',
                'details': {'raw': raw, 'elapsed_ms': elapsed_ms}}

    # tap_target shape を Layer2CliResult 互換に整形
    x = parsed.get('x')
    y = parsed.get('y')
    has_target = isinstance(x, (int, float)) and isinstance(y, (int, float))
    return {
        'tap_target': {'x': int(x), 'y': int(y)} if has_target else None,
        'confidence': float(parsed.get('confidence') or 0.0),
        'reason': parsed.get('reason', ''),
        'raw': raw,
        'elapsed_ms': elapsed_ms,
    }


def tool_classify_screen(args: dict) -> dict:
    args = args or {}
    ax_dump = args.get('ax_dump') or None
    image_path = args.get('image_path') or None
    timeout = int(args.get('timeout', 30) or 30)

    if not ax_dump and not image_path:
        return {'error': 'either ax_dump or image_path is required'}

    if ax_dump:
        source = 'ax_priority' if image_path else 'ax_only'
        messages = [
            {'role': 'system', 'content': CLASSIFY_SYSTEM_PROMPT},
            {'role': 'user', 'content': ax_dump},
        ]
    else:
        source = 'image_only'
        crop_bottom_pct = float(args.get('crop_bottom_pct', 0.08) or 0)
        effective_path = _preprocess_image_for_classify(image_path, crop_bottom_pct)
        try:
            with open(effective_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
        except Exception as e:
            return {'error': f'image read failed: {e}', 'details': {'image_path': image_path}}
        messages = [
            {'role': 'system', 'content': CLASSIFY_SYSTEM_PROMPT},
            {'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
                {'type': 'text', 'text': CLASSIFY_IMAGE_USER_PROMPT},
            ]},
        ]

    result = _classify_call(messages, timeout)
    if '_error' in result:
        details = {k: v for k, v in result.items() if k != '_error'}
        details['source'] = source
        return {'error': result['_error'], 'details': details}

    parsed = result['_parsed']
    return {
        'screen': parsed.get('screen'),
        'confidence': parsed.get('confidence'),
        'source': source,
        'elapsed_ms': result['_elapsed_ms'],
        'raw': result['_raw'],
    }


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
    'dismiss_screen': {
        'description': (
            'Detect close button coordinates in Android ad screenshot via local '
            'llama-server (Gemma E4B + mmproj on :8091). Replacement for '
            'layer2_cli.py (claude -p). Returns {tap_target:{x,y}|null, confidence, '
            'reason, raw, elapsed_ms}. AX verify is expected to be done by caller '
            '(C++ side via Layer2CliClient::verifyWithAx).'
        ),
        'schema': {'type': 'object', 'properties': {
            'image_path': {'type': 'string', 'description': 'Absolute path to PNG screenshot'},
            'frame_w':    {'type': 'integer', 'description': 'Image width in pixels (default 1200)'},
            'frame_h':    {'type': 'integer', 'description': 'Image height in pixels (default 2000)'},
            'timeout':    {'type': 'integer', 'description': 'HTTP timeout seconds (default 30)'},
        }, 'required': ['image_path']},
        'handler': tool_dismiss_screen,
    },
    'classify_screen': {
        'description': (
            'Classify an Android screen via local llama-server (Gemma E4B + mmproj on '
            'http://127.0.0.1:8091). Provide ax_dump (AX SUMMARY string, NOT raw XML) '
            'or image_path (PNG); both ok -> ax wins. '
            'Classes: ad, login, feed, settings, home, dialog, video. '
            'Returns {screen, confidence, source, elapsed_ms, raw}.'
        ),
        'schema': {'type': 'object', 'properties': {
            'ax_dump':    {'type': 'string', 'description': 'AX summary string. Caller summarizes the AX tree to a short hint line; raw XML will exceed ctx and likely misclassify. Example: "AX tree: FrameLayout root > FrameLayout ia_clickable_close_button clickable=true > TextView ad_label text=\\"AD\\" > Activity=TTFullScreenVideoActivity. Classify."'},
            'image_path': {'type': 'string', 'description': 'Absolute path to PNG screenshot'},
            'crop_bottom_pct': {'type': 'number', 'description': 'image_only: fraction of bottom to crop (default 0.08 = 8%, set to 0 to disable). Removes nav bar distractor.'},
            'timeout':    {'type': 'integer', 'description': 'HTTP timeout seconds (default 30)'},
        }},
        'handler': tool_classify_screen,
    },
}
