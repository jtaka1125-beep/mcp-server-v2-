"""
llm.py - LLM統一抽象層
=======================
全LLM呼び出しはここを通る。目的(purpose)に応じてモデルを選択。
Ollamaは使わない。

Purpose:
  'compact'  → qwen-3-235b優先（instruction following重視）
  'vision'   → Gemini Flash優先（画像理解）
  'code'     → Claude CLI優先（コード生成）
  'general'  → Groq 70B優先（汎用）
"""
import os
import time
import logging
import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 環境変数取得（HKCU + HKLM対応 / NTSYSTEMアカウント対応）
# ---------------------------------------------------------------------------
def _get_env(name: str) -> str:
    v = os.environ.get(name, '')
    if v:
        return v
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                sub = 'Environment' if hive == winreg.HKEY_CURRENT_USER else \
                      r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
                with winreg.OpenKey(hive, sub) as k:
                    v, _ = winreg.QueryValueEx(k, name)
                    if v:
                        return v
            except Exception:
                pass
    except ImportError:
        pass
    return ''

# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------
_USAGE_LOG = r'C:\MirageWork\mcp-server-v2\logs\llm_usage.log'

def _log_usage(backend: str, model: str, in_tok: int, out_tok: int,
               ok: bool, note: str = ''):
    try:
        os.makedirs(os.path.dirname(_USAGE_LOG), exist_ok=True)
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        status = 'OK' if ok else 'FAIL'
        line = f'{ts} | {backend:<10} | {model:<32} | in={in_tok:5} out={out_tok:5} total={in_tok+out_tok:6} | {status} | {note}\n'
        with open(_USAGE_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# バックエンド定義（purpose別の優先順位）
# ---------------------------------------------------------------------------
_BACKENDS = {
    'compact': ['cerebras_qwen', 'groq_70b', 'cerebras_8b'],
    'vision':  ['gemini_flash', 'groq_70b', 'cerebras_8b'],
    'code':    ['claude_cli',   'groq_70b', 'cerebras_8b'],
    'general': ['groq_70b',     'cerebras_qwen', 'cerebras_8b'],
}

# ---------------------------------------------------------------------------
# 各バックエンドの呼び出し実装
# ---------------------------------------------------------------------------

def _call_cerebras(model: str, prompt: str, max_tokens: int, timeout: int) -> str:
    key = _get_env('CEREBRAS_API_KEY')
    if not key:
        raise RuntimeError('CEREBRAS_API_KEY not set')
    resp = requests.post(
        'https://api.cerebras.ai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json',
                 'User-Agent': 'MirageSystem/1.0'},
        json={'model': model,
              'messages': [{'role': 'user', 'content': prompt}],
              'max_tokens': max_tokens},
        timeout=timeout,
    )
    resp.raise_for_status()
    rj = resp.json()
    usage = rj.get('usage', {})
    _log_usage('cerebras', model,
               usage.get('prompt_tokens', 0),
               usage.get('completion_tokens', 0), True)
    return rj['choices'][0]['message']['content'].strip()


def _call_groq(model: str, prompt: str, max_tokens: int, timeout: int) -> str:
    key = _get_env('GROQ_API_KEY')
    if not key:
        raise RuntimeError('GROQ_API_KEY not set')
    resp = requests.post(
        'https://api.groq.com/openai/v1/chat/completions',
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json',
                 'User-Agent': 'MirageSystem/1.0'},
        json={'model': model,
              'messages': [{'role': 'user', 'content': prompt}],
              'max_tokens': max_tokens},
        timeout=timeout,
    )
    resp.raise_for_status()
    rj = resp.json()
    usage = rj.get('usage', {})
    _log_usage('groq', model,
               usage.get('prompt_tokens', 0),
               usage.get('completion_tokens', 0), True)
    return rj['choices'][0]['message']['content'].strip()


def _call_gemini(prompt: str, max_tokens: int, timeout: int,
                 image_b64: str = None) -> str:
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError('google-generativeai not installed')
    key = _get_env('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY not set')
    genai.configure(api_key=key)
    model = genai.GenerativeModel(
        model_name='gemini-2.0-flash',
        generation_config=genai.types.GenerationConfig(
            temperature=0.1, max_output_tokens=max_tokens)
    )
    parts = []
    if image_b64:
        import base64
        parts.append({'mime_type': 'image/jpeg',
                      'data': base64.b64decode(image_b64)})
    parts.append(prompt)
    resp = model.generate_content(parts,
                                  request_options={'timeout': timeout})
    _log_usage('gemini', 'gemini-2.0-flash',
               len(prompt) // 4, len(resp.text) // 4, True)
    return resp.text.strip()


def _call_claude_cli(prompt: str, timeout: int) -> str:
    import subprocess, shutil
    claude = shutil.which('claude') or r'C:\Users\jun\.local\bin\claude.EXE'
    if not os.path.exists(claude):
        raise RuntimeError('claude CLI not found')
    env = os.environ.copy()
    env.update({
        'USERPROFILE':       r'C:\Users\jun',
        'APPDATA':           r'C:\Users\jun\AppData\Roaming',
        'LOCALAPPDATA':      r'C:\Users\jun\AppData\Local',
        'HOMEDRIVE':         'C:',
        'HOMEPATH':          r'\Users\jun',
        'CLAUDE_CONFIG_DIR': r'C:\Users\jun\.claude',
    })
    result = subprocess.run(
        [claude, '--print', '-p', prompt],
        capture_output=True, text=True, timeout=timeout, env=env,
        cwd=r'C:\MirageWork\mcp-server-v2',
    )
    if result.returncode != 0:
        raise RuntimeError(f'claude CLI rc={result.returncode}: {result.stderr[:100]}')
    _log_usage('claude_cli', 'claude-sonnet',
               len(prompt) // 4, len(result.stdout) // 4, True)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# バックエンド名 → 呼び出し関数のマッピング
# ---------------------------------------------------------------------------
def _dispatch(backend: str, prompt: str, max_tokens: int,
              timeout: int, **kwargs) -> str:
    if backend == 'cerebras_qwen':
        return _call_cerebras('qwen-3-235b-a22b-instruct-2507',
                              prompt, max_tokens, timeout)
    elif backend == 'cerebras_8b':
        return _call_cerebras('llama3.1-8b', prompt, max_tokens, timeout)
    elif backend == 'groq_70b':
        return _call_groq('llama-3.3-70b-versatile', prompt, max_tokens, timeout)
    elif backend == 'gemini_flash':
        return _call_gemini(prompt, max_tokens, timeout,
                            image_b64=kwargs.get('image_b64'))
    elif backend == 'claude_cli':
        return _call_claude_cli(prompt, timeout)
    else:
        raise ValueError(f'Unknown backend: {backend}')


# ---------------------------------------------------------------------------
# 公開API
# ---------------------------------------------------------------------------
def call(prompt: str,
         purpose: str = 'general',
         max_tokens: int = 800,
         timeout: int = 30,
         **kwargs) -> str:
    """
    LLM呼び出しの唯一の入口。

    Args:
        prompt:     プロンプト文字列
        purpose:    'compact' | 'vision' | 'code' | 'general'
        max_tokens: 最大出力トークン数
        timeout:    タイムアウト秒数
        **kwargs:   image_b64 (vision用) など

    Returns:
        LLMの応答文字列。全バックエンド失敗時は空文字列。
    """
    backends = _BACKENDS.get(purpose, _BACKENDS['general'])
    last_err = None

    for backend in backends:
        try:
            log.debug(f'llm.call purpose={purpose} backend={backend}')
            result = _dispatch(backend, prompt, max_tokens, timeout, **kwargs)
            if result:
                return result
        except Exception as e:
            last_err = e
            _log_usage(backend, '?', 0, 0, False, str(e)[:80])
            log.warning(f'llm.call backend={backend} failed: {e}')
            continue

    log.error(f'llm.call all backends failed for purpose={purpose}: {last_err}')
    return ''
