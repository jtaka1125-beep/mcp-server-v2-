"""
memory/compact.py - compactロジック
=====================================
Ollamaは一切使わない。全LLM呼び出しはllm.call()経由。
"""
import re
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import llm
from config import COMPACT_LABELS

# ---------------------------------------------------------------------------
# プロンプトテンプレート
# ---------------------------------------------------------------------------
def _build_prompt(text: str, namespace: str, max_chars: int) -> str:
    # namespace別のラベルヒント
    hints = {
        'mx-design':      '主に [設計][理由][commit][廃止] を使え。',
        'mx-log':         '主に [TODO][実装][バグ][保留][確認待] を使え。',
        'mirage-android': '主に [設計][実装][デバイス][禁止][commit] を使え。',
        'mirage-infra':   '主に [実装][環境][パス][TODO][バグ] を使え。',
        'mirage-vulkan':  '主に [設計][実装][禁止][commit][バグ] を使え。',
    }
    label_hint = hints.get(namespace, '')
    labels_str = ' '.join(COMPACT_LABELS)

    return f"""以下はMirageSystemの開発ログです。重要な情報を抽出し、必ず以下のフォーマットで出力してください。

【出力フォーマット（厳守）】
[ラベル] キーワード: 内容

【使用するラベル】
{labels_str}

【ルール】
- 1行1項目。散文・見出し（**text**）・箇条書き（-や*）は絶対禁止
- 必ず[ラベル]で始める
- 最大{min(max_chars, 800)}文字
- [禁止]タグの項目は必ず保持する
- {label_hint}
- MirageSystemの説明は不要（読者は開発者）

## 開発ログ
{text}

上記を[ラベル] キーワード: 内容 形式で出力:"""

# ---------------------------------------------------------------------------
# 後処理: 散文→ラベル形式に強制変換
# ---------------------------------------------------------------------------
def _normalize(raw: str, max_chars: int = 800) -> str:
    label_re = re.compile(r'^\[.+?\]\s+\S')
    lines = raw.strip().splitlines()

    # すでにラベル形式が半数以上 → そのまま
    label_count = sum(1 for l in lines if label_re.match(l.strip()))
    if len(lines) > 0 and label_count / len(lines) >= 0.5:
        return raw[:max_chars]

    result = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 見出し行をスキップ
        if line.startswith('##') or line.startswith('---'):
            continue
        if line.startswith('**') and line.endswith('**'):
            continue
        # 既にラベル形式
        if label_re.match(line):
            result.append(line)
            continue
        # 箇条書き記号を除去
        line = re.sub(r'^[*\-+]\s+', '', line)
        line = re.sub(r'^\d+\.\s+', '', line)
        if not line:
            continue
        # キーワードでラベル推定
        if any(w in line for w in ['禁止', 'H264', 'AOA', 'D3D11', 'banned']):
            result.append(f'[禁止] {line}')
        elif any(w in line for w in ['TODO', 'やること', '未解決', '次に']):
            result.append(f'[TODO] {line}')
        elif any(w in line for w in ['完了', '実装', '修正', 'commit']):
            result.append(f'[実装] {line}')
        elif any(w in line for w in ['設計', '方針', '決定', 'アーキ']):
            result.append(f'[設計] {line}')
        elif any(w in line for w in ['保留', '後回し', '動作確認後']):
            result.append(f'[保留] {line}')
        elif any(w in line for w in ['バグ', '問題', '不具合', 'エラー']):
            result.append(f'[バグ] {line}')
        else:
            result.append(f'[実装] {line}')

    return '\n'.join(result)[:max_chars]

# ---------------------------------------------------------------------------
# メイン: compact実行
# ---------------------------------------------------------------------------
def run(namespace: str, msgs: list, max_chars: int = 800) -> dict:
    """
    messagesリストを受け取り、ラベル形式のbootstrapを返す。

    Args:
        namespace: メモリnamespace
        msgs:      fetch_recent_rawの結果
        max_chars: 最大文字数

    Returns:
        {'bootstrap': str, 'error': str|None}
    """
    # mx-constは対象外
    if namespace == 'mx-const':
        return {'bootstrap': '', 'error': 'mx-const is permanent'}

    if not msgs:
        return {'bootstrap': '', 'error': 'no messages'}

    # テキスト構築
    text = '\n---\n'.join(
        f"[{m.get('role','?')}] {m.get('content','')[:300]}"
        for m in msgs[:20]
    )

    prompt = _build_prompt(text, namespace, max_chars)
    raw = llm.call(prompt, purpose='compact', max_tokens=800, timeout=30)

    if not raw:
        return {'bootstrap': '', 'error': 'all LLM backends failed'}

    # 後処理
    bootstrap = _normalize(raw, max_chars)
    return {'bootstrap': bootstrap, 'error': None}
