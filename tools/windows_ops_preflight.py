"""
preflight.py - Verify Windows-MCP integration compatibility for mcp-server-v2.

Run with the same Python that mcp-server-v2 uses:
    python C:\\MirageWork\\mcp-server-v2\\tools\\windows_ops_preflight.py

Outputs:
  PASS -> direct-import mode is safe; set USE_SUBPROCESS_FALLBACK = False.
  FAIL -> see error; likely need USE_SUBPROCESS_FALLBACK = True.

Exit code is 0 on PASS, 1 on FAIL. Prints diagnostics either way.
"""
from __future__ import annotations

import os
import sys
import traceback


WIN_MCP_ROOT = (
    r"C:\Users\jun\AppData\Roaming\Claude\Claude Extensions"
    r"\ant.dir.cursortouch.windows-mcp"
)


def banner(label: str) -> None:
    print()
    print("=" * 60)
    print(label)
    print("=" * 60)


def main() -> int:
    banner("PREFLIGHT: Windows-MCP direct-import compatibility")

    print(f"Host Python  : {sys.version}")
    print(f"Executable   : {sys.executable}")

    venv_site = os.path.join(WIN_MCP_ROOT, ".venv", "Lib", "site-packages")
    win_src = os.path.join(WIN_MCP_ROOT, "src")
    venv_py = os.path.join(WIN_MCP_ROOT, ".venv", "Scripts", "python.exe")

    print(f"Win-MCP root : {WIN_MCP_ROOT}")
    print(f"  venv site  : {venv_site}  exists={os.path.exists(venv_site)}")
    print(f"  src        : {win_src}     exists={os.path.exists(win_src)}")
    print(f"  venv py    : {venv_py}     exists={os.path.exists(venv_py)}")

    if not os.path.exists(venv_site) or not os.path.exists(win_src):
        print()
        print("[FAIL] Windows-MCP paths missing; cannot proceed.")
        return 1

    # Read the venv Python's version marker (via directory name or sysconfig file)
    venv_pyconfig = os.path.join(WIN_MCP_ROOT, ".venv", "pyvenv.cfg")
    if os.path.exists(venv_pyconfig):
        print()
        print("Win-MCP venv config:")
        with open(venv_pyconfig, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                print(f"  {line.rstrip()}")

    banner("Attempting direct import of windows_mcp.desktop.service.Desktop")

    sys.path.insert(0, venv_site)
    sys.path.insert(0, win_src)

    try:
        import windows_mcp  # type: ignore
        print(f"[ok ] windows_mcp package: {windows_mcp.__file__}")
    except Exception as e:
        print(f"[FAIL] import windows_mcp failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    try:
        from windows_mcp.desktop.service import Desktop  # type: ignore
        print("[ok ] from windows_mcp.desktop.service import Desktop")
    except Exception as e:
        print(f"[FAIL] import Desktop failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    try:
        d = Desktop()
        print(f"[ok ] Desktop() instance: {d}")
    except Exception as e:
        print(f"[FAIL] Desktop() init failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    try:
        size = d.get_screen_size()
        print(f"[ok ] get_screen_size(): {size}")
    except Exception as e:
        print(f"[WARN] get_screen_size failed (not fatal): {type(e).__name__}: {e}")

    try:
        img = d.get_screenshot()
        print(f"[ok ] get_screenshot(): {img.size} mode={img.mode}")
    except Exception as e:
        print(f"[WARN] get_screenshot failed: {type(e).__name__}: {e}")
        traceback.print_exc()

    try:
        import pyperclip  # type: ignore
        print(f"[ok ] pyperclip import: {pyperclip.__file__}")
    except Exception as e:
        print(f"[WARN] pyperclip unavailable: {e}")

    try:
        import psutil  # type: ignore
        print(f"[ok ] psutil import: {psutil.__version__}")
    except Exception as e:
        print(f"[WARN] psutil unavailable: {e}")

    banner("RESULT")
    print("[PASS] Direct-import mode is safe.")
    print("  -> In windows_ops.py keep USE_SUBPROCESS_FALLBACK = False")
    print("  -> Rename windows_ops.py.draft -> windows_ops.py")
    print("  -> Add import line to server.py, then restart mcp-v2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
