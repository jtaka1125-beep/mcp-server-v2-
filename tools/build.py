"""
tools/build.py - ビルド・GUI系ツール
"""
import os
import sys
import subprocess
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import MIRAGE_DIR

log = logging.getLogger(__name__)

BUILD_DIR = os.path.join(MIRAGE_DIR, 'build')

# ---------------------------------------------------------------------------
# build_mirage
# ---------------------------------------------------------------------------
def tool_build_mirage(args: dict) -> dict:
    target  = (args or {}).get('target', 'mirage_vulkan')
    jobs    = int((args or {}).get('jobs', 4) or 4)
    timeout = int((args or {}).get('timeout', 300) or 300)

    # 実行中のGUIをkill（Permission denied防止）
    subprocess.run('taskkill /F /IM mirage_vulkan.exe 2>nul',
                   shell=True, capture_output=True)
    time.sleep(2)

    cmd = (f'cmake --build "{BUILD_DIR}" --config Release '
           f'--target {target} -j{jobs}')
    try:
        t0 = time.perf_counter()
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=MIRAGE_DIR,
            encoding='utf-8', errors='replace',
        )
        elapsed = time.perf_counter() - t0
        ok = r.returncode == 0
        # エラー行だけ抽出
        lines = (r.stdout + r.stderr).splitlines()
        errors = [l for l in lines if 'error:' in l.lower()][:20]
        return {
            'ok': ok,
            'exit_code': r.returncode,
            'elapsed_sec': round(elapsed, 1),
            'errors': errors,
            'tail': lines[-10:] if lines else [],
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'build timeout after {timeout}s'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

# ---------------------------------------------------------------------------
# run_mirage_gui
# ---------------------------------------------------------------------------
def tool_run_mirage_gui(args: dict) -> dict:
    try:
        r = subprocess.run(
            'schtasks /run /tn MirageLaunch',
            shell=True, capture_output=True, text=True, timeout=10,
        )
        ok = r.returncode == 0
        return {
            'status': 'launched' if ok else 'failed',
            'rc': r.returncode,
            'out': r.stdout.strip(),
        }
    except Exception as e:
        return {'status': 'error', 'error': str(e)}

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
TOOLS = {
    'build_mirage': {
        'description': 'Build MirageVulkan project (cmake, target mirage_vulkan).',
        'schema': {'type': 'object', 'properties': {
            'target': {'type': 'string'},
            'jobs': {'type': 'integer'},
            'timeout': {'type': 'integer'},
        }},
        'handler': tool_build_mirage,
    },
    'run_mirage_gui': {
        'description': 'Run MirageGUI application via scheduled task.',
        'schema': {'type': 'object', 'properties': {}},
        'handler': tool_run_mirage_gui,
    },
}
