"""
config.py - 設定一元管理
"""
import os

# ---------------------------------------------------------------------------
# ポート
# ---------------------------------------------------------------------------
PORT_NEW      = 3001   # mcp-server-v2（メイン）
PORT_FALLBACK = 3000   # mcp-server（旧・フォールバック）

# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------
BASE_DIR     = r'C:\MirageWork\mcp-server-v2'
LOG_DIR      = os.path.join(BASE_DIR, 'logs')
DATA_DIR     = r'C:\MirageWork\mcp-server\data'      # 既存DBを共有
MEMORY_DB    = os.path.join(DATA_DIR, 'memory.db')

MIRAGE_DIR   = r'C:\MirageWork\MirageVulkan'
ADB_EXE      = r'C:\Users\jun\AppData\Local\Android\Sdk\platform-tools\adb.exe'
CLAUDE_EXE   = r'C:\Users\jun\.local\bin\claude.EXE'

# ---------------------------------------------------------------------------
# デバイス構成
# ---------------------------------------------------------------------------
DEVICES = {
    'x1':    {'wifi': '192.168.0.10:5555', 'res_w': 1080, 'res_h': 1800,
              'phys_w': 1200, 'phys_h': 2000, 'hub': 'hub1',
              'port_cmd': 50000, 'port_img': 50001},
    'a9_956':{'wifi': '192.168.0.7:5555',  'res_w': 800,  'res_h': 1340,
              'hub': 'hub3', 'port_cmd': 50100, 'port_img': 50101},
    'a9_479':{'wifi': '192.168.0.6:5555',  'res_w': 800,  'res_h': 1340,
              'hub': 'hub2', 'port_cmd': 50200, 'port_img': 50201},
}

# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
MEMORY_NAMESPACES = [
    'mx-const', 'mx-design', 'mx-log',
    'mirage-vulkan', 'mirage-android', 'mirage-infra',
    'mirage-design', 'mirage-general',
]

COMPACT_LABELS = [
    '[禁止]', '[設計]', '[実装]', '[commit]', '[ファイル]',
    '[デバイス]', '[パス]', '[TODO]', '[理由]', '[廃止]',
    '[バグ]', '[保留]', '[環境]', '[確認待]',
]
