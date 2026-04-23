# Windows ops integration checklist (updated 2026-04-20 19:55)

## IMPORTANT finding from preflight (2026-04-20, Jun 出張中)

Direct-import mode is **NOT viable** as-is:
  - mcp-server-v2 runs on Python 3.12 (Windows Store)
  - Windows-MCP's .venv was built against Python 3.13 (uv-managed)
  - pywin32 DLLs are ABI-incompatible between the two
  - Also discovered: the uv-managed Python 3.13 that backs the .venv
    is **no longer on disk** (folder `C:\Users\jun\AppData\Roaming\uv`
    gone), even though PID 24996 is still running from it (exe kept
    open but file deleted).
  - Consequence: the venv's `python.exe` shim can't execute anymore
    ("did not find executable at ...cpython-3.13.12...").

Subprocess fallback (via Windows-MCP's venv python) ALSO fails for
the same reason (python shim broken). So `windows_ops.py.draft` is
written correctly for subprocess mode but cannot be activated until
the underlying Python is restored.

## Homecoming workflow (Jun's machine, physically present)

### Option 1: Let Claude Desktop self-heal (cheapest, try first)

  1. Exit Claude Desktop (system tray -> Quit)
  2. Reopen Claude Desktop
  3. It should detect the broken Windows-MCP venv and reinstall
     (observe Claude Extensions logs).
  4. Run preflight to check:
     ```powershell
     & "C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0\python3.12.exe" C:\MirageWork\mcp-server-v2\tools\windows_ops_preflight.py
     ```
     -> expect FAIL at the Desktop() line (ABI mismatch 3.12 vs 3.13)
        BUT the subprocess body in windows_ops.py.draft will now work
        since the venv python is back.
  5. Self-test the draft (this uses subprocess, 3.12 host is fine):
     ```powershell
     & "C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0\python3.12.exe" C:\MirageWork\mcp-server-v2\tools\windows_ops.py.draft
     ```
     Expect: 3 self-tests pass (screenshot, clipboard, process list).

### Option 2: Rebuild venv manually with uv

  **PowerShell, not cmd. Copy line-by-line to avoid smartphone typo hell.**

  ```powershell
  # 1. Ensure uv python 3.13 is installed (idempotent, can rerun)
  uv python install 3.13

  # 2. Exit Claude Desktop first (system tray > Quit), otherwise step 3
  #    will fail because PID 24996 has the venv dir open.

  # 3. Remove the broken venv
  Remove-Item -Recurse -Force "C:\Users\jun\AppData\Roaming\Claude\Claude Extensions\ant.dir.cursortouch.windows-mcp\.venv"

  # 4. Create a new venv (--python with TWO dashes, not three)
  uv venv --python 3.13 "C:\Users\jun\AppData\Roaming\Claude\Claude Extensions\ant.dir.cursortouch.windows-mcp\.venv"

  # 5. cd into the Windows-MCP extension dir
  cd "C:\Users\jun\AppData\Roaming\Claude\Claude Extensions\ant.dir.cursortouch.windows-mcp"

  # 6. Sync dependencies from pyproject.toml / uv.lock
  uv sync

  # 7. Run preflight + self-test (as in Option 1 step 4 & 5)
  ```

  After this, restart Claude Desktop and verify Windows-MCP extension
  comes back up cleanly.

### Option 3: Give up on Windows-MCP venv, bootstrap our own

  Install the needed packages into mcp-server-v2's python directly:
  ```powershell
  & "C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0\python3.12.exe" -m pip install --user uiautomation pywin32 pillow dxcam pyperclip psutil
  ```
  (`uiautomation` on PyPI = yinkaisheng, same code as vendored in
  `windows_mcp.uia`.)

  Then edit windows_ops.py to NOT touch the Windows-MCP venv at all
  — write a minimal direct-call version using `uiautomation.*`
  primitives. Bigger rewrite; avoid unless Option 1/2 both fail.

## Activation (after preflight passes)

```powershell
cd C:\MirageWork\mcp-server-v2\tools
Rename-Item windows_ops.py.draft windows_ops.py
```

Then edit `server.py` L74 area to add:
```python
import tools.windows_ops as winops_tools; TOOLS.update(winops_tools.TOOLS)
```

Restart v2:
```powershell
# Find v2 PID (port 3001)
netstat -ano | findstr :3001
# taskkill /F /PID <pid>
schtasks /Run /TN MirageMCPServerV2
# Wait 5s
netstat -ano | findstr :3001   # confirm back up
```

Smoke test via curl:
```powershell
curl -s -X POST http://localhost:3001/mcp `
  -H "Content-Type: application/json" `
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' `
  | Select-String windows_
```
Expect to see all the windows_* tool names.

Then one working call:
```powershell
curl -s -X POST http://localhost:3001/mcp `
  -H "Content-Type: application/json" `
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"windows_wait","arguments":{"duration":1}}}'
```
This one uses no subprocess, should return `{"ok":true,"slept_sec":1}` fast.

## Rollback

```powershell
cd C:\MirageWork\mcp-server-v2
git diff server.py   # review
git checkout -- server.py  # revert if not yet committed
Rename-Item tools\windows_ops.py tools\windows_ops.py.draft
```

mcp-v2 restart then proceeds without the Windows ops bundle; all other
tools remain unaffected.

## Files in this package (uncommitted, .draft held)

  tools/windows_ops.py.draft       (~28KB; subprocess-based, ready)
  tools/windows_ops_preflight.py   (~4KB; compat checker)
  tools/WINDOWS_OPS_CHECKLIST.md   (this file)

## Common smartphone-typo gotchas (Jun 出張中だから重要)

  - `---python` (3 dashes) -> `--python` (2 dashes, UV は 2 本)
  - `(cd to Win-MCP root)` is a PLACEHOLDER not a literal command,
    replace with the actual `cd "C:\Users\jun\AppData\...\windows-mcp"`
  - PowerShell vs cmd: `Remove-Item -Recurse -Force` (PS) vs
    `rmdir /S /Q` (cmd). Pick one, don't mix.
  - Paths with spaces need double quotes.
