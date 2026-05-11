"""
windows_ops.py — Windows desktop operations for mcp-server-v2.

Dispatches all calls to Windows-MCP's Python 3.13 venv via subprocess.
Chosen because mcp-server-v2 runs on Python 3.12 (Windows Store) and
Windows-MCP's pywin32 DLLs are built for 3.13 — ABI-incompatible direct import.

Overhead: ~500ms-1s per call (pywin32 cold-start + comtypes init).
Acceptable for single-shot screenshot / snapshot / click; click-storms should
be batched into one snapshot + plan.

-----------------------------------------------------------------------------
STATUS (2026-04-20): DRAFT, NOT YET REGISTERED WITH server.py
Preflight result: direct-import FAILED (Py3.13 venv vs Py3.12 host ABI).
                   Subprocess fallback confirmed as the active strategy.
-----------------------------------------------------------------------------

To activate:
  1. cd C:\\MirageWork\\mcp-server-v2\\tools
  2. move windows_ops.py.draft windows_ops.py
  3. In server.py around L74 add:
         import tools.windows_ops as winops_tools; TOOLS.update(winops_tools.TOOLS)
  4. Smoke test standalone:
         python windows_ops.py  # runs a self-test (screenshot dimensions)
  5. Restart mcp-v2:
         taskkill /F /PID <v2>; schtasks /Run /TN MirageMCPServerV2
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import sys
from typing import Any

log = logging.getLogger("windows_ops")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_WIN_MCP_ROOT = (
    r"C:\Users\jun\AppData\Roaming\Claude\Claude Extensions"
    r"\ant.dir.cursortouch.windows-mcp"
)
_WIN_MCP_PYTHON = os.path.join(_WIN_MCP_ROOT, ".venv", "Scripts", "python.exe")
_WIN_MCP_SITE = os.path.join(_WIN_MCP_ROOT, ".venv", "Lib", "site-packages")
_WIN_MCP_SRC = os.path.join(_WIN_MCP_ROOT, "src")

_DEFAULT_TIMEOUT = 30  # seconds per subprocess call

REQUIRE_CONFIRM = {
    "windows_filesystem:delete": "File/directory deletion is not reversible.",
    "windows_process:kill": "Killing a critical process can break MV or MCP-server.",
    "windows_registry:hklm-write": "HKLM changes affect the entire system.",
}

# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

_RUNNER_PREAMBLE = f"""
import sys, json, base64, io, os, traceback
sys.path.insert(0, r'{_WIN_MCP_SITE}')
sys.path.insert(0, r'{_WIN_MCP_SRC}')
"""


def _run_in_venv(body: str, *, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """Execute `body` inside Windows-MCP's venv. Body must print exactly one
    JSON object on stdout ({"ok": true, ...} or {"error": "..."})."""
    if not os.path.exists(_WIN_MCP_PYTHON):
        return {"error": f"win-mcp venv python not found at {_WIN_MCP_PYTHON}"}

    program = _RUNNER_PREAMBLE + "\n" + "try:\n"
    # Indent body by 4 spaces so it lives in the try:.
    for line in body.splitlines():
        program += "    " + line + "\n"
    program += (
        "except Exception as _e:\n"
        "    print(json.dumps({'error': type(_e).__name__ + ': ' + str(_e),\n"
        "                      'traceback': traceback.format_exc()}))\n"
    )

    try:
        r = subprocess.run(
            [_WIN_MCP_PYTHON, "-c", program],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"error": f"subprocess launch: {e}"}

    if r.returncode != 0 and not r.stdout.strip():
        return {
            "error": f"subprocess exited {r.returncode}",
            "stderr": r.stderr[-2000:],
        }

    out = r.stdout.strip()
    # In case multiple prints snuck in, take the last JSON-looking line.
    last_brace = out.rfind("\n{")
    if last_brace >= 0:
        out = out[last_brace + 1 :]
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {
            "error": "subprocess returned non-JSON",
            "stdout": r.stdout[-2000:],
            "stderr": r.stderr[-2000:],
        }


def _confirm(op_key: str, args: dict) -> dict | None:
    if op_key in REQUIRE_CONFIRM and not bool(args.get("confirmed", False)):
        return {
            "error": "confirmation_required",
            "op": op_key,
            "reason": REQUIRE_CONFIRM[op_key],
            "hint": "Re-call with confirmed=True after user approves.",
        }
    return None


def _j(value: Any) -> str:
    """Serialize a Python value as a Python literal usable in a code body.

    [FIX 2026-04-24] Was json.dumps(), which produced JSON tokens (null/true/false)
    invalid in Python -- caused NameError in windows_snapshot when display=None.
    Switched to repr() so None/True/False serialize as valid Python literals.
    dict/list/str/int/float all remain evalable as Python.
    """
    return repr(value)


# ===========================================================================
# Handlers (each builds a small body and ships it to the venv)
# ===========================================================================

def tool_windows_screenshot(args: dict) -> dict:
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        "d = Desktop()\n"
        "img = d.get_screenshot()\n"
        "buf = io.BytesIO()\n"
        "img.save(buf, format='PNG', optimize=True)\n"
        "data = buf.getvalue()\n"
        "print(json.dumps({\n"
        "  'ok': True,\n"
        "  'format': 'png',\n"
        "  'width': img.width,\n"
        "  'height': img.height,\n"
        "  'size_bytes': len(data),\n"
        "  'image_b64': base64.b64encode(data).decode('ascii'),\n"
        "}))\n"
    )
    return _run_in_venv(body, timeout=20)


def tool_windows_snapshot(args: dict) -> dict:
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        "from windows_mcp.tools._snapshot_helpers import (\n"
        "    capture_desktop_state, build_snapshot_response\n"
        ")\n"
        f"use_ui_tree = {bool(args.get('use_ui_tree', True))}\n"
        f"use_annotation = {bool(args.get('use_annotation', False))}\n"
        f"use_dom = {bool(args.get('use_dom', False))}\n"
        f"display = {_j(args.get('display'))}\n"
        f"width_reference_line = {_j(args.get('width_reference_line'))}\n"
        f"height_reference_line = {_j(args.get('height_reference_line'))}\n"
        "d = Desktop()\n"
        "result = capture_desktop_state(\n"
        "    d, use_vision=False, use_dom=use_dom,\n"
        "    use_annotation=use_annotation, use_ui_tree=use_ui_tree,\n"
        "    display=display, tool_name='windows_snapshot',\n"
        "    width_reference_line=width_reference_line,\n"
        "    height_reference_line=height_reference_line,\n"
        ")\n"
        "payload = build_snapshot_response(result, include_ui_details=True)\n"
        "out = []\n"
        "for item in payload:\n"
        "    if hasattr(item, 'text'):\n"
        "        out.append({'type': 'text', 'text': item.text})\n"
        "    elif hasattr(item, 'data') and hasattr(item, 'mimeType'):\n"
        "        out.append({'type': 'image', 'mime': item.mimeType, 'data_b64': item.data})\n"
        "    else:\n"
        "        out.append({'type': 'raw', 'repr': repr(item)})\n"
        "print(json.dumps({'ok': True, 'snapshot': out}))\n"
    )
    # [FIX 2026-04-24] 45s->120s: UIA enumeration can hang when apps are
    # launching / not responding to WM messages (see windows-mcp service.py L154).
    return _run_in_venv(body, timeout=120)


def tool_windows_click(args: dict) -> dict:
    loc = args.get("loc")
    if not loc or len(loc) != 2:
        return {"error": "loc required as [x, y]"}
    button = args.get("button", "left")
    clicks = int(args.get("clicks", 1))
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        f"Desktop().click({tuple(loc)}, button={button!r}, clicks={clicks})\n"
        f"print(json.dumps({{'ok': True, 'loc': {list(loc)!r}, 'button': {button!r}, 'clicks': {clicks}}}))\n"
    )
    return _run_in_venv(body, timeout=15)


def tool_windows_type(args: dict) -> dict:
    text = args.get("text")
    if text is None:
        return {"error": "text required"}
    loc = args.get("loc")
    clear = bool(args.get("clear", False))
    press_enter = bool(args.get("press_enter", False))
    caret_position = args.get("caret_position", "idle")
    if caret_position not in ("start", "idle", "end"):
        return {"error": "caret_position must be start|idle|end"}

    if loc is None:
        # Type-at-cursor: use uia.SendKeys directly.
        body = (
            "from windows_mcp.uia import SendKeys\n"
            "from windows_mcp.desktop.service import _escape_text_for_sendkeys\n"
            f"text = {text!r}\n"
            f"press_enter = {press_enter}\n"
            "SendKeys(_escape_text_for_sendkeys(text), interval=0.02, waitTime=0.05)\n"
            "if press_enter:\n"
            "    SendKeys('{Enter}', waitTime=0.05)\n"
            "print(json.dumps({'ok': True, 'typed': len(text), 'loc': None}))\n"
        )
    else:
        body = (
            "from windows_mcp.desktop.service import Desktop\n"
            f"loc = {tuple(loc)!r}\n"
            f"Desktop().type(text={text!r}, loc=loc,\n"
            f"               caret_position={caret_position!r},\n"
            f"               clear={clear}, press_enter={press_enter})\n"
            f"print(json.dumps({{'ok': True, 'typed': len({text!r}), 'loc': {list(loc)!r}}}))\n"
        )
    return _run_in_venv(body, timeout=30)


def tool_windows_shortcut(args: dict) -> dict:
    shortcut = args.get("shortcut")
    if not shortcut:
        return {"error": "shortcut required"}
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        f"Desktop().shortcut({shortcut!r})\n"
        f"print(json.dumps({{'ok': True, 'shortcut': {shortcut!r}}}))\n"
    )
    return _run_in_venv(body, timeout=15)


def tool_windows_scroll(args: dict) -> dict:
    loc = args.get("loc")
    direction = args.get("direction", "down")
    scroll_type = args.get("type", "vertical")
    wheel_times = int(args.get("wheel_times", 1))
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        f"loc = {tuple(loc)!r} if {loc!r} else None\n"
        f"err = Desktop().scroll(loc=loc, type={scroll_type!r},\n"
        f"                      direction={direction!r},\n"
        f"                      wheel_times={wheel_times})\n"
        "if err:\n"
        "    print(json.dumps({'error': err}))\n"
        "else:\n"
        f"    print(json.dumps({{'ok': True, 'direction': {direction!r}, 'wheel_times': {wheel_times}}}))\n"
    )
    return _run_in_venv(body, timeout=15)


def tool_windows_move(args: dict) -> dict:
    loc = args.get("loc")
    if not loc or len(loc) != 2:
        return {"error": "loc required as [x, y]"}
    drag = bool(args.get("drag", False))
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        "d = Desktop()\n"
        f"drag = {drag}\n"
        f"loc = {tuple(loc)!r}\n"
        "if drag:\n"
        "    d.drag(loc)\n"
        "else:\n"
        "    d.move(loc)\n"
        f"print(json.dumps({{'ok': True, 'loc': {list(loc)!r}, 'drag': {drag}}}))\n"
    )
    return _run_in_venv(body, timeout=15)


def tool_windows_app(args: dict) -> dict:
    mode = args.get("mode", "launch")
    name = args.get("name")
    if mode in ("launch", "switch") and not name:
        return {"error": f"name required for {mode}"}

    if mode == "launch":
        body = (
            "from windows_mcp.desktop.service import Desktop\n"
            f"r = Desktop().launch_app({name!r})\n"
            f"print(json.dumps({{'ok': True, 'mode': 'launch', 'name': {name!r}, 'result': list(r) if r else None}}))\n"
        )
    elif mode == "switch":
        body = (
            "from windows_mcp.desktop.service import Desktop\n"
            f"Desktop().switch_app({name!r})\n"
            f"print(json.dumps({{'ok': True, 'mode': 'switch', 'name': {name!r}}}))\n"
        )
    elif mode == "resize":
        return {"error": "resize mode not yet wired"}
    else:
        return {"error": f"unknown mode: {mode}"}
    return _run_in_venv(body, timeout=30)


def tool_windows_clipboard(args: dict) -> dict:
    mode = args.get("mode")
    if mode == "get":
        body = (
            "import pyperclip\n"
            "t = pyperclip.paste()\n"
            "print(json.dumps({'ok': True, 'text': t, 'length': len(t)}))\n"
        )
    elif mode == "set":
        text = args.get("text", "")
        body = (
            "import pyperclip\n"
            f"t = {text!r}\n"
            "pyperclip.copy(t)\n"
            "print(json.dumps({'ok': True, 'written': len(t)}))\n"
        )
    else:
        return {"error": "mode must be 'get' or 'set'"}
    return _run_in_venv(body, timeout=10)


def tool_windows_wait(args: dict) -> dict:
    # Sleep locally; no need for subprocess.
    import time
    duration = max(0, min(int(args.get("duration", 1)), 60))
    time.sleep(duration)
    return {"ok": True, "slept_sec": duration}


def tool_windows_process(args: dict) -> dict:
    mode = args.get("mode", "list")
    if mode == "list":
        sort_by = args.get("sort_by", "memory")
        limit = int(args.get("limit", 20))
        body = (
            "import psutil\n"
            f"sort_by = {sort_by!r}\n"
            f"limit = {limit}\n"
            "procs = []\n"
            "for p in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_percent']):\n"
            "    try:\n"
            "        info = p.info\n"
            "        mem_mb = round(info['memory_info'].rss / (1024*1024), 1) if info.get('memory_info') else 0\n"
            "        procs.append({'pid': info['pid'], 'name': info.get('name') or '?',\n"
            "                      'mem_mb': mem_mb, 'cpu': info.get('cpu_percent') or 0})\n"
            "    except Exception: pass\n"
            "key_fn = {'memory': (lambda x: x['mem_mb']),\n"
            "          'cpu':    (lambda x: x['cpu']),\n"
            "          'name':   (lambda x: x['name'])}.get(sort_by, lambda x: x['mem_mb'])\n"
            "procs.sort(key=key_fn, reverse=sort_by in ('memory','cpu'))\n"
            "print(json.dumps({'ok': True, 'processes': procs[:limit]}))\n"
        )
        return _run_in_venv(body, timeout=15)

    elif mode == "kill":
        err = _confirm("windows_process:kill", args)
        if err:
            return err
        pid = args.get("pid")
        name = args.get("name")
        body = (
            "import psutil\n"
            f"pid = {pid!r}\n"
            f"name = {name!r}\n"
            "killed, errors = [], []\n"
            "if pid:\n"
            "    try: psutil.Process(int(pid)).kill(); killed.append(int(pid))\n"
            "    except Exception as e: errors.append('pid=%s: %s' % (pid, e))\n"
            "if name:\n"
            "    for p in psutil.process_iter(['pid','name']):\n"
            "        try:\n"
            "            if p.info.get('name') == name:\n"
            "                p.kill(); killed.append(p.pid)\n"
            "        except Exception: pass\n"
            "print(json.dumps({'ok': True, 'killed': killed, 'errors': errors}))\n"
        )
        return _run_in_venv(body, timeout=15)
    else:
        return {"error": f"unknown mode: {mode}"}


def tool_windows_notification(args: dict) -> dict:
    title = args.get("title", "Mirage")
    message = args.get("message", "")
    app_id = args.get("app_id", "Mirage")
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        f"r = Desktop().send_notification(title={title!r}, message={message!r}, app_id={app_id!r})\n"
        "print(json.dumps({'ok': True, 'result': str(r) if r else None}))\n"
    )
    return _run_in_venv(body, timeout=15)


def tool_windows_registry(args: dict) -> dict:
    mode = args.get("mode")
    path = args.get("path")
    name = args.get("name")
    if not path:
        return {"error": "path required"}
    if path.upper().startswith("HKLM") and mode in ("set", "delete"):
        err = _confirm("windows_registry:hklm-write", args)
        if err:
            return err

    if mode == "get":
        if not name:
            return {"error": "name required for get"}
        body = (
            "from windows_mcp.desktop.service import Desktop\n"
            f"print(json.dumps({{'ok': True, 'value': Desktop().registry_get(path={path!r}, name={name!r})}}))\n"
        )
    elif mode == "set":
        value = args.get("value")
        reg_type = args.get("type", "String")
        if not name or value is None:
            return {"error": "name and value required"}
        body = (
            "from windows_mcp.desktop.service import Desktop\n"
            f"r = Desktop().registry_set(path={path!r}, name={name!r}, value={value!r}, reg_type={reg_type!r})\n"
            "print(json.dumps({'ok': True, 'result': r}))\n"
        )
    elif mode == "delete":
        body = (
            "from windows_mcp.desktop.service import Desktop\n"
            f"r = Desktop().registry_delete(path={path!r}, name={name!r})\n"
            "print(json.dumps({'ok': True, 'result': r}))\n"
        )
    elif mode == "list":
        body = (
            "from windows_mcp.desktop.service import Desktop\n"
            f"r = Desktop().registry_list(path={path!r})\n"
            "print(json.dumps({'ok': True, 'result': r}))\n"
        )
    else:
        return {"error": f"unknown mode: {mode}"}
    return _run_in_venv(body, timeout=15)


def tool_windows_filesystem(args: dict) -> dict:
    mode = args.get("mode")
    path = args.get("path")
    if not mode or not path:
        return {"error": "mode and path required"}
    # Path-traversal validation (mirror system.py:_validate_path).
    # path is mandatory; dst_path is used by copy/move and is also validated.
    for key in ("path", "dst_path"):
        v = args.get(key)
        if not v:
            continue
        if ".." in str(v).replace("\\", "/").split("/"):
            return {"error": f"{key} traversal denied (.. segment): {v!r}"}
    if mode == "delete":
        err = _confirm("windows_filesystem:delete", args)
        if err:
            return err

    # Serialize the arg dict and let the venv-side code pick matching kwargs.
    args_j = _j(args)
    body = (
        "import inspect\n"
        "from windows_mcp import filesystem as fs\n"
        f"args = {args_j}\n"
        "mode = args.get('mode')\n"
        "fn_map = {\n"
        "  'read':   getattr(fs, 'read_file', None) or getattr(fs, 'read', None),\n"
        "  'write':  getattr(fs, 'write_file', None) or getattr(fs, 'write', None),\n"
        "  'copy':   getattr(fs, 'copy', None),\n"
        "  'move':   getattr(fs, 'move', None),\n"
        "  'delete': getattr(fs, 'delete', None),\n"
        "  'list':   getattr(fs, 'list_directory', None) or getattr(fs, 'list', None),\n"
        "  'search': getattr(fs, 'search', None),\n"
        "  'info':   getattr(fs, 'info', None),\n"
        "}\n"
        "fn = fn_map.get(mode)\n"
        "if fn is None:\n"
        "    print(json.dumps({'error': 'mode %s unavailable' % mode}))\n"
        "else:\n"
        "    sig = inspect.signature(fn)\n"
        "    kwargs = {k: v for k, v in args.items() if k in sig.parameters}\n"
        "    if 'path' in sig.parameters and 'path' not in kwargs:\n"
        "        kwargs['path'] = args.get('path')\n"
        "    r = fn(**kwargs)\n"
        "    print(json.dumps({'ok': True, 'mode': mode, 'result': str(r) if r is not None else None}))\n"
    )
    return _run_in_venv(body, timeout=30)


def tool_windows_scrape(args: dict) -> dict:
    url = args.get("url")
    if not url:
        return {"error": "url required"}
    body = (
        "from windows_mcp.desktop.service import Desktop\n"
        f"r = Desktop().scrape({url!r})\n"
        "t = r if isinstance(r, str) else str(r)\n"
        "print(json.dumps({'ok': True, 'length': len(t), 'text': t[:20000]}))\n"
    )
    return _run_in_venv(body, timeout=60)


# ===========================================================================
# TOOLS registry
# ===========================================================================

TOOLS = {
    "windows_screenshot": {
        "description": "Capture the Windows desktop as a PNG. Returns base64.",
        "schema": {"type": "object", "properties": {}},
        "handler": tool_windows_screenshot,
    },
    "windows_snapshot": {
        "description": "Return UI tree + clickable element coordinates. Preferred over screenshot when planning a click/type.",
        "schema": {
            "type": "object",
            "properties": {
                "use_ui_tree": {"type": "boolean", "default": True},
                "use_annotation": {"type": "boolean", "default": False},
                "use_dom": {"type": "boolean", "default": False},
                "display": {"type": "array", "items": {"type": "integer"}},
            },
        },
        "handler": tool_windows_snapshot,
    },
    "windows_click": {
        "description": "Click at [x, y]. Free for routine clicks. For purchase/payment, confirm with the user first.",
        "schema": {
            "type": "object",
            "properties": {
                "loc": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
            },
            "required": ["loc"],
        },
        "handler": tool_windows_click,
    },
    "windows_type": {
        "description": "Type text, optionally clicking a location first. Avoid credentials/card numbers without explicit approval.",
        "schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "loc": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "clear": {"type": "boolean", "default": False},
                "press_enter": {"type": "boolean", "default": False},
                "caret_position": {"type": "string", "enum": ["start", "idle", "end"], "default": "idle"},
            },
            "required": ["text"],
        },
        "handler": tool_windows_type,
    },
    "windows_shortcut": {
        "description": "Send a keyboard shortcut: 'ctrl+c', 'alt+tab', 'win+d', etc.",
        "schema": {
            "type": "object",
            "properties": {"shortcut": {"type": "string"}},
            "required": ["shortcut"],
        },
        "handler": tool_windows_shortcut,
    },
    "windows_scroll": {
        "description": "Scroll up/down/left/right.",
        "schema": {
            "type": "object",
            "properties": {
                "loc": {"type": "array", "items": {"type": "integer"}},
                "type": {"type": "string", "enum": ["horizontal", "vertical"], "default": "vertical"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
                "wheel_times": {"type": "integer", "default": 1},
            },
        },
        "handler": tool_windows_scroll,
    },
    "windows_move": {
        "description": "Move cursor to [x, y] (optionally drag from current pos).",
        "schema": {
            "type": "object",
            "properties": {
                "loc": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                "drag": {"type": "boolean", "default": False},
            },
            "required": ["loc"],
        },
        "handler": tool_windows_move,
    },
    "windows_app": {
        "description": "Launch or switch focus to a Windows app. mode=launch needs a name matched against Start Menu.",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["launch", "resize", "switch"], "default": "launch"},
                "name": {"type": "string"},
            },
        },
        "handler": tool_windows_app,
    },
    "windows_clipboard": {
        "description": "Get or set Windows clipboard text.",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["get", "set"]},
                "text": {"type": "string"},
            },
            "required": ["mode"],
        },
        "handler": tool_windows_clipboard,
    },
    "windows_wait": {
        "description": "Sleep for N seconds (cap 60). Runs locally in mcp-v2, no subprocess.",
        "schema": {
            "type": "object",
            "properties": {"duration": {"type": "integer", "default": 1, "minimum": 0, "maximum": 60}},
        },
        "handler": tool_windows_wait,
    },
    "windows_process": {
        "description": "List or kill Windows processes. Kill needs confirmed=True (easy to break MV/MCP by accident).",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["list", "kill"], "default": "list"},
                "name": {"type": "string"},
                "pid": {"type": "integer"},
                "sort_by": {"type": "string", "enum": ["memory", "cpu", "name"], "default": "memory"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 500},
                "confirmed": {"type": "boolean", "default": False},
            },
        },
        "handler": tool_windows_process,
    },
    "windows_notification": {
        "description": "Send a Windows toast notification.",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "message": {"type": "string"},
                "app_id": {"type": "string", "default": "Mirage"},
            },
            "required": ["title", "message"],
        },
        "handler": tool_windows_notification,
    },
    "windows_registry": {
        "description": "Read/write Windows registry. HKLM set/delete needs confirmed=True.",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["get", "set", "delete", "list"]},
                "path": {"type": "string", "description": "e.g. 'HKCU:\\\\Software\\\\Mirage'"},
                "name": {"type": "string"},
                "value": {"type": "string"},
                "type": {"type": "string", "enum": ["String", "DWord", "QWord", "Binary", "MultiString", "ExpandString"], "default": "String"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["mode", "path"],
        },
        "handler": tool_windows_registry,
    },
    "windows_filesystem": {
        "description": "File ops: read/write/copy/move/delete/list/search/info. delete needs confirmed=True.",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["read", "write", "copy", "move", "delete", "list", "search", "info"]},
                "path": {"type": "string"},
                "destination": {"type": "string"},
                "content": {"type": "string"},
                "pattern": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
                "append": {"type": "boolean", "default": False},
                "overwrite": {"type": "boolean", "default": False},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
                "encoding": {"type": "string", "default": "utf-8"},
                "show_hidden": {"type": "boolean", "default": False},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["mode", "path"],
        },
        "handler": tool_windows_filesystem,
    },
    "windows_scrape": {
        "description": "Scrape an active browser page (read-only, truncated to 20k chars).",
        "schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "handler": tool_windows_scrape,
    },
}


# ---------------------------------------------------------------------------
# Self-test: run with `python windows_ops.py.draft` (or .py after rename)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    print("self-test: windows_screenshot")
    r = tool_windows_screenshot({})
    if r.get("ok"):
        print(f"  OK width={r['width']} height={r['height']} size={r['size_bytes']}B")
    else:
        print(f"  FAIL: {r}")
        sys.exit(1)

    print("self-test: windows_clipboard get")
    r = tool_windows_clipboard({"mode": "get"})
    if r.get("ok"):
        print(f"  OK length={r['length']}")
    else:
        print(f"  FAIL: {r}")

    print("self-test: windows_process list --limit 5")
    r = tool_windows_process({"mode": "list", "limit": 5, "sort_by": "memory"})
    if r.get("ok"):
        print(f"  OK top-memory procs: {[p['name'] for p in r['processes'][:5]]}")
    else:
        print(f"  FAIL: {r}")

    print("self-test complete")
