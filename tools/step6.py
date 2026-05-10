"""step6_run: Single Step 6 cycle (Stage 1 + crop + Stage 2 + save) via `claude -p`.

Architecture (2026-04-29 confirmation, post-POC):
- No interactive Claude Code window
- No clipboard / SendKeys
- No recorder (chat Claude orchestrator)
- MCP server orchestrates deterministically; AI judgment is purely the
  LLM call inside `claude -p` subprocess.

Flow:
  1. Stage 1 = bias-free ad judgment, returns crop_paths if is_ad
  2. (CC autonomously did PIL.crop in Stage 1 subprocess, file already exists)
  3. Stage 2 = detailed extraction + save (fresh subprocess, separate context)
  4. Returns merged JSON + log directory

Each stage is a fresh `claude -p` invocation. No `/clear` needed
(separate subprocesses = automatic context separation).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, Optional


LOG_ROOT = r"C:\MirageWork\step6_logs"
DEFAULT_TIMEOUT_SEC = 180

# Pattern for fenced JSON code blocks (CC often wraps response in ```json ... ```)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    """Extract a JSON object from CC's text response.

    Tries fenced code block first, then falls back to first '{' position.
    """
    if not text:
        return None
    m = _FENCED_JSON_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: scan for balanced braces from first '{'
    start = text.find('{')
    while start >= 0:
        # Try greedy parse to end-of-string
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError as e:
            # Trim to suspected JSON end if parser hints at position
            if hasattr(e, 'pos') and e.pos > 0:
                try:
                    return json.loads(text[start:start + e.pos])
                except json.JSONDecodeError:
                    pass
            start = text.find('{', start + 1)
    return None


def _build_stage1_prompt(scene_path: str, width: int, height: int) -> str:
    return f"""[単発画像判定タスク]
- CLAUDE.md の MANDATORY FIRST STEP は適用外
- memory_bootstrap / memory_search 等の外部記憶アクセスは不要
- ツール呼び出しは最小限に、画像と JSON 出力のみ

@{scene_path}
画像実寸は {width}x{height} です。

この画像を見て JSON で:
{{ "is_ad": bool,
  "confidence": float,
  "tap_target": {{"x": int, "y": int}} | null,
  "target2_overlap": {{"x1": int, "y1": int, "x2": int, "y2": int}} | null,
  "crop_paths": [str, ...] | null,
  "reason": str }}

is_ad = true の場合、応答を返す前に target2_overlap 領域から
Bash で python -c "from PIL import Image; im=Image.open(r'{scene_path}'); ..." 等を使い
PIL.crop で画像を切り出して一時ファイルに保存し、その絶対パスを
crop_paths に列挙すること (1-3 枚目安)。
保存先 directory: C:\\MirageWork\\MirageVulkan\\scratch\\step6_crop\\
ファイル名: <YYYYMMDD_HHMMSS>_crop_N.png

判定基準:
- is_ad = true: 広告オーバーレイ、× ボタン、Skip ボタン、PR 表記等
- is_ad = false: 通常 UI、他フィールドは null (crop_paths も null)

confidence: 判定の確信度 (0.0-1.0)、reason に根拠を簡潔に。
タップは実行しないでください、座標を返すだけ。
最終応答は JSON のみ、前後のテキスト・説明文を入れないこと。
"""


# ---------------------------------------------------------------------------
# [Phase F] AX (Accessibility) dump fetch via macro_api 'ui_tree' RPC
# ---------------------------------------------------------------------------

def _fetch_ax_dump(device_id: str, save_path: str, timeout_s: float = 5.0) -> bool:
    """Call macro_api 'ui_tree' RPC and save AccessibilityNodeInfo tree JSON.

    Returns True if file saved with valid JSON, False on failure.
    Caller may proceed without AX info; CC will fall back to OCR/image_diff_overlap.
    """
    import socket as _sock
    try:
        req = {"id": 1, "method": "ui_tree", "params": {"device_id": device_id}}
        req_bytes = (json.dumps(req) + "\n").encode("utf-8")
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.settimeout(timeout_s)
        s.connect(("127.0.0.1", 19840))
        s.sendall(req_bytes)
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        s.close()
        if b"\n" not in buf:
            return False
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", errors="replace"))
        result = resp.get("result", {})
        if result.get("status") != "ok":
            return False
        ui_tree = result.get("ui_tree")
        if ui_tree is None:
            return False
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(ui_tree, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _build_stage2_prompt(crop_path: str, ax_dump_path: Optional[str] = None) -> str:
    ax_block = ""
    if ax_dump_path and os.path.exists(ax_dump_path):
        ax_block = f"""
[Phase F] AccessibilityNodeInfo tree (利用可能):
@{ax_dump_path}
このファイルを Read してください。target2_overlap 領域内に clickable=true で
resource_id を持つノードがあれば、target.type=resource_id を優先してください。
clickable=true で text を持つノードしかなければ target.type=text を選択。
どちらもなければ既存の ocr / image_diff_overlap にフォールバック。
"""

    return f"""[単発画像判定タスク (継続)]
- CLAUDE.md の MANDATORY FIRST STEP は適用外
- 外部記憶アクセス不要、画像と JSON 出力のみ

これは 1 回目で広告と判定した画像を、target2_overlap 領域で
CC 自身が crop した画像です:
@{crop_path}
crop 画像の実寸は PIL.Image.open で自分で取得してください。
{ax_block}
以下の JSON で詳細を返してください:

{{ "detail_coords": {{"x": int, "y": int}} | null,
  "ocr_text": str,
  "template_metadata": {{
    // [Phase F] AX 由来 resource_id/text があれば target を出力 (type=resource_id|text)
    // ない場合は target1/target2 の既存 schema (type=ocr|image_diff_overlap) を使う
    "target": {{
      "type": "resource_id" | "text",
      "value": str,
      "tap_offset": {{"dx": int, "dy": int}}
    }} | null,
    "target1": {{
      "type": "ocr",
      "patterns": [list of str],
      "tap_offset": {{"dx": int, "dy": int}}
    }},
    "target2": {{
      "type": "image_diff_overlap",
      "region": {{"x1": int, "y1": int, "x2": int, "y2": int}},
      "comment": str
    }},
    "save_path": "C:\\\\MirageWork\\\\MirageVulkan\\\\templates\\\\auto\\\\<timestamp>_<short_id>.png"
  }}
}}

template_metadata.save_path の path に PNG (target2 region の crop 画像) と
同 stem の metadata JSON を CC 自身が直接保存し、応答 JSON のトップレベルに
"saved" フィールドを追加:
  "saved": {{"png_path": str, "json_path": str, "sha256": str, "size": int}}

タップは実行しないでください、座標とメタデータを返すだけ。
最終応答は JSON のみ、前後のテキスト・説明文を入れないこと。
"""


def _run_claude_p(prompt: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    """Invoke `claude -p` and return parsed wrapper + extracted inner JSON."""
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "elapsed_sec": timeout}
    except FileNotFoundError:
        return {"error": "claude_cli_not_found"}

    if proc.returncode != 0:
        return {
            "error": "non_zero_exit",
            "returncode": proc.returncode,
            "stderr_head": (proc.stderr or "")[:500],
        }

    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "error": "wrapper_parse_failed",
            "stdout_head": (proc.stdout or "")[:500],
        }

    if wrapper.get("is_error"):
        return {
            "error": "claude_api_error",
            "result": wrapper.get("result"),
            "duration_ms": wrapper.get("duration_ms"),
        }

    result_text = wrapper.get("result", "") or ""
    inner = _extract_json(result_text)
    if inner is None:
        return {
            "error": "result_parse_failed",
            "result_head": result_text[:500],
            "duration_ms": wrapper.get("duration_ms"),
        }

    return {
        "ok": True,
        "result": inner,
        "raw_text": result_text,
        "duration_ms": wrapper.get("duration_ms"),
        "cost_usd": wrapper.get("total_cost_usd"),
    }


def _save_log(log_dir: str, name: str, content: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, name), "w", encoding="utf-8") as f:
        f.write(content)


def tool_step6_run(args: dict) -> dict:
    """Run a single Step 6 cycle via `claude -p` subprocesses.

    Args:
        scene_path: Full screen PNG absolute path
        device_id:  Android device id (eg 192.168.0.10:5555)
        width:      Image width (auto-detected via PIL if 0/missing)
        height:     Image height (auto-detected via PIL if 0/missing)

    Returns:
        dict with status, stage1, stage2, log_dir, timing/cost.
        status values: completed | completed_no_ad | stage1_failed |
                       stage1_no_crop | crop_file_missing | stage2_failed | error
    """
    scene_path = (args or {}).get("scene_path", "") or ""
    device_id  = (args or {}).get("device_id", "unknown") or "unknown"
    width      = int((args or {}).get("width") or 0)
    height     = int((args or {}).get("height") or 0)

    if not scene_path:
        return {"status": "error", "error": "scene_path required"}
    if not os.path.exists(scene_path):
        return {"status": "error", "error": "scene_path not found", "scene_path": scene_path}

    # Auto-detect dimensions if missing (stdlib only - PNG header parse)
    if not width or not height:
        try:
            import struct
            with open(scene_path, "rb") as f:
                sig = f.read(8)
                if sig[:8] != b"\x89PNG\r\n\x1a\n":
                    raise ValueError("not a PNG file")
                f.read(4)  # IHDR length
                if f.read(4) != b"IHDR":
                    raise ValueError("missing IHDR chunk")
                w, h = struct.unpack(">II", f.read(8))
                width, height = int(w), int(h)
        except Exception as e:
            return {"status": "error", "error": f"image dimensions unavailable: {e}"}

    # Per-cycle log directory
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_dev = device_id.replace(":", "-").replace("/", "-").replace("\\", "-")
    log_dir = os.path.join(LOG_ROOT, f"{ts}_{safe_dev}")

    cycle_started = time.time()

    # =====================================
    # Stage 1: bias-free judgment + (autonomous) crop
    # =====================================
    prompt1 = _build_stage1_prompt(scene_path, width, height)
    _save_log(log_dir, "01_stage1_prompt.txt", prompt1)

    r1 = _run_claude_p(prompt1)
    _save_log(
        log_dir, "02_stage1_raw.txt",
        r1.get("raw_text") or json.dumps(r1, ensure_ascii=False, indent=2),
    )

    if not r1.get("ok"):
        result = {
            "status": "stage1_failed",
            "error": r1.get("error"),
            "stage1_raw": r1,
            "log_dir": log_dir,
        }
        _save_log(log_dir, "99_meta.json", json.dumps(result, ensure_ascii=False, indent=2))
        return result

    stage1 = r1["result"]
    _save_log(log_dir, "03_stage1_parsed.json", json.dumps(stage1, ensure_ascii=False, indent=2))

    # is_ad = false → stop here
    if not stage1.get("is_ad"):
        result = {
            "status": "completed_no_ad",
            "stage1": stage1,
            "stage1_duration_ms": r1.get("duration_ms"),
            "stage1_cost_usd": r1.get("cost_usd"),
            "total_duration_ms": int((time.time() - cycle_started) * 1000),
            "log_dir": log_dir,
        }
        _save_log(log_dir, "99_meta.json", json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # Need crop_paths for Stage 2
    crop_paths = stage1.get("crop_paths") or []
    if not crop_paths:
        result = {
            "status": "stage1_no_crop",
            "stage1": stage1,
            "note": "is_ad=true だが crop_paths が空、Stage 2 をスキップ",
            "log_dir": log_dir,
        }
        _save_log(log_dir, "99_meta.json", json.dumps(result, ensure_ascii=False, indent=2))
        return result

    crop_path = crop_paths[0]
    if not os.path.exists(crop_path):
        result = {
            "status": "crop_file_missing",
            "stage1": stage1,
            "crop_path": crop_path,
            "log_dir": log_dir,
            "note": "CC が crop_paths に列挙したが file が存在しない",
        }
        _save_log(log_dir, "99_meta.json", json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # =====================================
    # Stage 2: detailed extraction + save (fresh subprocess)
    # =====================================
    # [Phase F] Fetch AX dump for resource_id/text-based targets
    ax_dump_path = os.path.join(log_dir, "03_ax_dump.json")
    ax_ok = _fetch_ax_dump(device_id, ax_dump_path)
    if ax_ok:
        _save_log(log_dir, "03_ax_dump_ok.txt", f"AX dump saved: {ax_dump_path}")
    else:
        ax_dump_path = None  # CC は AX なしで OCR/image_diff_overlap fallback

    prompt2 = _build_stage2_prompt(crop_path, ax_dump_path)
    _save_log(log_dir, "04_stage2_prompt.txt", prompt2)

    r2 = _run_claude_p(prompt2)
    _save_log(
        log_dir, "05_stage2_raw.txt",
        r2.get("raw_text") or json.dumps(r2, ensure_ascii=False, indent=2),
    )

    if not r2.get("ok"):
        result = {
            "status": "stage2_failed",
            "stage1": stage1,
            "stage2_error": r2.get("error"),
            "stage2_raw": r2,
            "log_dir": log_dir,
        }
        _save_log(log_dir, "99_meta.json", json.dumps(result, ensure_ascii=False, indent=2))
        return result

    stage2 = r2["result"]
    _save_log(log_dir, "06_stage2_parsed.json", json.dumps(stage2, ensure_ascii=False, indent=2))

    # Done
    result = {
        "status": "completed",
        "stage1": stage1,
        "stage2": stage2,
        "stage1_duration_ms": r1.get("duration_ms"),
        "stage2_duration_ms": r2.get("duration_ms"),
        "total_duration_ms": int((time.time() - cycle_started) * 1000),
        "stage1_cost_usd": r1.get("cost_usd"),
        "stage2_cost_usd": r2.get("cost_usd"),
        "total_cost_usd": (r1.get("cost_usd") or 0) + (r2.get("cost_usd") or 0),
        "log_dir": log_dir,
    }
    _save_log(log_dir, "99_meta.json", json.dumps(result, ensure_ascii=False, indent=2))
    return result


TOOLS: Dict[str, Any] = {
    "step6_run": {
        "description": (
            "Run a single Step 6 cycle (Stage 1 + crop + Stage 2 + save) via "
            "`claude -p` subprocess. No interactive CC window, no clipboard, "
            "no recorder. Each stage is a fresh subprocess (auto context "
            "separation). Returns judgment JSON + log directory path."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "scene_path": {
                    "type": "string",
                    "description": "Full-screen PNG absolute path",
                },
                "device_id": {
                    "type": "string",
                    "description": "Android device id (eg 192.168.0.10:5555)",
                },
                "width": {
                    "type": "integer",
                    "description": "Image width (auto-detected via PIL if 0)",
                },
                "height": {
                    "type": "integer",
                    "description": "Image height (auto-detected via PIL if 0)",
                },
            },
            "required": ["scene_path", "device_id"],
        },
        "handler": tool_step6_run,
    },
}
