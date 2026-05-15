"""
memory/compact.py v3 (2026-04-26)
=====================================
変更点:
  - 時系列セクション必須 (直近 N 日の動向、日付付き)
  - meta_bootstrap entry と統一フォーマット (テーマ別セクション)
  - 既存 bootstrap を継承 (増分更新)
  - msgs 拡大、max_chars 拡大
  - 「構造や方式に統一感を持たせて時系列が繋がる」(Jun 2026-04-26)

LLM 呼び出しは全て llm.call() 経由 (Ollama 不使用)。
"""
import re
import sys
import os
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import llm

# ---------------------------------------------------------------------------
# プロンプトテンプレート v3
# ---------------------------------------------------------------------------
def _build_prompt_v3(text: str, namespace: str, max_chars: int,
                     prev_summary: str = '') -> str:
    """v3: 時系列 + テーマ別 + 増分更新対応。

    前回 summary を「過去要約」として LLM に渡し、新規分との統合を依頼する。
    出力は meta_bootstrap entry と同じ階層構造に揃える。
    """
    prev_section = ''
    if prev_summary:
        prev_section = (
            "\n## 前回の要約 (継承元、必要に応じて統合)\n"
            f"{prev_summary[:1500]}\n"
        )

    return f"""以下は MirageSystem の {namespace} namespace の開発ログです。
{namespace} 全体の状況を把握できる構造化サマリを生成してください。

【出力フォーマット (厳守、meta_bootstrap entry と統一)】

■ アクティブテーマ
  _theme:xxx  簡潔な説明 (1 行)
  _theme:yyy  簡潔な説明
  ...
  (3-7 個程度。idx_themes_master のカノニカル名と整合)

■ 直近の重要トピック (時系列、新→古)
  ## YYYY-MM-DD
    トピック 1 (1 行)
    トピック 2
  ## YYYY-MM-DD
    ...
  (直近 14 日が目安、日付ごとにグループ化)

■ 主要設計判断 (現役)
  - 判断 1
  - 判断 2
  ...
  (5-8 個以内、意味的重複を統合してまとめる)

■ 既知問題 / 残課題
  - 問題 1 (1 行)
  - 問題 2

【ルール】
- 全体で {max_chars} 文字以内
- 各行は簡潔に (1 行 80 文字目安)
- 散文は禁止、上記構造のみ
- {namespace} に無関係な内容は含めない
- 前回要約を踏まえて増分更新する (廃止項目は削除、新規項目は追加)
- 古い決定が新しい決定で覆されている場合、新しい方を残す
- 日付は ## YYYY-MM-DD 形式で必ず付ける
- 日付不明な行は ## (日付不明) でグループ化
- **「主要設計判断」での意味的重複を必ず統合する**:
  - 同じ機能領域・同じ目的の判断は語が違っても 1 つに集約
  - 例: "fastembed の開発" / "memory_fastembed の開発" / "memory_link_health の開発" /
    "MCPサーバーの信頼性改善" のような並列名は「外部記憶基盤 (fastembed/link_health 等) の信頼性強化」のように 1 行に統合
  - 5-8 件以内に収まらない場合は、より上位の概念で括る
  - 前回要約に重複が残っていても、本回出力では統合し直す
- **「主要設計判断」と「既知問題 / 残課題」は相互排他**:
  - 完了済み・現役で機能している → 主要設計判断のみ
  - 未解決・継続課題・改善余地あり → 既知問題のみ
  - 同じトピックが両方に該当しそうなら、状態で振り分ける:
    - 「整備完了 + 一部改善余地」→ 主要設計判断 (改善点はコメントで併記しない、別行が必要なら既知問題側に具体的な未完項目だけ書く)
    - 「整備中 / テンプレート不足」→ 既知問題のみ
  - 同名項目が両セクションに居る要約は必ずどちらか一方に集約してから出力
{prev_section}

## 開発ログ (新→古、{len(text)} 文字)
{text}

上記フォーマットで出力 (見出し ■ から始める):"""


# ---------------------------------------------------------------------------
# 後処理 v3
# ---------------------------------------------------------------------------
def _dedupe_cross_section(raw: str) -> str:
    """主要設計判断 と 既知問題 / 残課題 が同名項目を両方持つのを post-process で除去。
    LLM (qwen) が prev_summary の重複を踏襲して両セクションに同じ行を残してしまうため、
    programmatic に保証する。重複時は 既知問題側を削除 (主要設計判断 = 現役、を優先)。
    """
    lines = raw.splitlines()
    SEC_DESIGN = '主要設計判断'
    SEC_ISSUES = '既知問題'

    def _line_key(line: str) -> str:
        # 先頭の "-" "  -" "・" "  " を剥がして本文だけ取り出し、空白圧縮
        s = line.lstrip()
        for prefix in ('- ', '・', '* '):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        return ''.join(s.split())

    # セクション境界を ■ で検出
    sec_idx = []  # [(line_idx, section_name)]
    for i, line in enumerate(lines):
        if line.lstrip().startswith('■'):
            sec_idx.append((i, line))

    # 主要設計判断 の中身行を集める
    design_items = set()
    for j, (start, header) in enumerate(sec_idx):
        if SEC_DESIGN not in header:
            continue
        end = sec_idx[j + 1][0] if j + 1 < len(sec_idx) else len(lines)
        for k in range(start + 1, end):
            body = lines[k].strip()
            if body and not body.startswith('■') and not body.startswith('##'):
                design_items.add(_line_key(lines[k]))

    # 既知問題 セクションで design_items と一致する行を削除
    out = list(lines)
    removed = 0
    for j, (start, header) in enumerate(sec_idx):
        if SEC_ISSUES not in header:
            continue
        end = sec_idx[j + 1][0] if j + 1 < len(sec_idx) else len(lines)
        for k in range(start + 1, end):
            body = lines[k].strip()
            if not body or body.startswith('■') or body.startswith('##'):
                continue
            if _line_key(lines[k]) in design_items:
                out[k] = None  # mark for removal
                removed += 1

    return '\n'.join(l for l in out if l is not None)


def _normalize_v3(raw: str, max_chars: int) -> str:
    """v3 normalize: ■ で始まる構造を保持、見出し以外の行は許容。"""
    # Post-process: cross-section dedup (主要設計判断 vs 既知問題)
    raw = _dedupe_cross_section(raw)
    lines = raw.strip().splitlines()

    # ■ で始まる行が 1 つもなければ failure
    has_section = any(line.strip().startswith('■') for line in lines)
    if not has_section:
        # 構造が壊れている場合、先頭に ■ を補う
        return ('■ 概要 (フォーマット崩壊、要再 compact)\n' + raw)[:max_chars]

    # 余分な空行除去 (連続 2 行以上の空行を 1 行に)
    result = []
    blank_count = 0
    for line in lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 1:
                result.append('')
        else:
            blank_count = 0
            result.append(line)

    joined = '\n'.join(result)
    if max_chars and len(joined) > max_chars:
        cut = joined[:max_chars]
        for sep in ('\n', '。', '、'):
            idx = cut.rfind(sep)
            if idx > max_chars // 2:
                return cut[:idx + len(sep)].rstrip() + ' …'
        return cut.rstrip() + ' …'
    return joined


# ---------------------------------------------------------------------------
# msgs を時系列ヘッダ付きテキストに整形
# ---------------------------------------------------------------------------
def _format_msgs_with_dates(msgs: list, max_msgs: int = 50) -> str:
    """msgs (created_at 含む) を日付グループ化された string に変換。"""
    grouped = {}  # date_str -> list of contents
    for m in msgs[:max_msgs]:
        ts = m.get('created_at', 0)
        if ts:
            try:
                date_str = datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d')
            except Exception:
                date_str = '日付不明'
        else:
            date_str = '日付不明'

        role = m.get('role', '?')
        # [Fix 2026-05-15] 300 → 800 chars
        #   旧 300 char cap で decision body 1500+ chars が title 部分のみ抽出、
        #   LLM 入力時に詳細 (= 修正内容/影響) が欠落、 summary timeline で
        #   旧 prev_summary 内容を上書きできない bug の二次原因。
        #   memory: mirage-infra 94edecfa layer 2 fix (writer truncation)
        content = m.get('content', '')[:800]
        grouped.setdefault(date_str, []).append(f"  [{role}] {content}")

    # 新→古 順
    sorted_dates = sorted(
        [d for d in grouped.keys() if d != '日付不明'],
        reverse=True
    )
    if '日付不明' in grouped:
        sorted_dates.append('日付不明')

    parts = []
    for date_str in sorted_dates:
        parts.append(f"## {date_str}")
        parts.extend(grouped[date_str])
        parts.append('')

    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# メイン: compact 実行 v3
# ---------------------------------------------------------------------------
def run(namespace: str, msgs: list, max_chars: int = 2000,
        prev_summary: str = '') -> dict:
    """
    msgs リストを受け取り、構造化された bootstrap を返す。

    v3 改修:
      - 時系列セクション必須
      - 既存 summary を継承して増分更新
      - max_chars デフォルト 2000 (旧 800)

    Args:
        namespace:    メモリ namespace
        msgs:         fetch_recent_raw + decisions の混在 (created_at 含む)
        max_chars:    最大文字数 (デフォルト 2000)
        prev_summary: 前回 bootstrap (増分更新用、空文字なら全件再生成)

    Returns:
        {'bootstrap': str, 'error': str|None}
    """
    if namespace == 'mx-const':
        return {'bootstrap': '', 'error': 'mx-const is permanent'}

    if not msgs:
        return {'bootstrap': '', 'error': 'no messages'}

    # 時系列ヘッダ付きで整形 (最大 50 msg)
    text = _format_msgs_with_dates(msgs, max_msgs=50)

    prompt = _build_prompt_v3(text, namespace, max_chars, prev_summary)

    # max_tokens を max_chars に応じて拡大
    max_tokens = max(1500, max_chars + 500)
    raw = llm.call(prompt, purpose='compact_v3',
                   max_tokens=max_tokens, timeout=60)

    if not raw:
        return {'bootstrap': '', 'error': 'all LLM backends failed'}

    bootstrap = _normalize_v3(raw, max_chars)
    return {'bootstrap': bootstrap, 'error': None}
