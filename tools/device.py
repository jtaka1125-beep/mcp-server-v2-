"""
tools/device.py - デバイス・ADB系ツール
=========================================
ADB操作、スクリーンショット、USBハブ制御。
"""
import os
import sys
import subprocess
import base64
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import ADB_EXE, DEVICES

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADB helper
# ---------------------------------------------------------------------------
def _adb(args: list, timeout: int = 15) -> tuple[bool, str, str]:
    """ADBコマンドを実行する。(ok, stdout, stderr)を返す。"""
    cmd = [ADB_EXE] + args
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, encoding='utf-8', errors='replace',
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, '', f'timeout after {timeout}s'
    except Exception as e:
        return False, '', str(e)

# ---------------------------------------------------------------------------
# adb_devices
# ---------------------------------------------------------------------------
def tool_adb_devices(args: dict) -> dict:
    ok, stdout, stderr = _adb(['devices', '-l'])
    lines = [l for l in stdout.splitlines() if l and 'List of devices' not in l]
    devices = []
    for l in lines:
        parts = l.split()
        if len(parts) >= 2:
            devices.append({'serial': parts[0], 'state': parts[1],
                            'info': ' '.join(parts[2:])})
    return {'devices': devices, 'count': len(devices), 'raw': stdout}

# ---------------------------------------------------------------------------
# adb_shell
# ---------------------------------------------------------------------------
def tool_adb_shell(args: dict) -> dict:
    device  = (args or {}).get('device', '')
    command = (args or {}).get('command', '')
    timeout = int((args or {}).get('timeout', 15) or 15)
    if not command:
        return {'error': 'command required'}
    adb_args = (['-s', device] if device else []) + ['shell', command]
    ok, stdout, stderr = _adb(adb_args, timeout=timeout)
    return {'ok': ok, 'output': stdout, 'stderr': stderr}

# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------
def tool_screenshot(args: dict) -> dict:
    device    = (args or {}).get('device', '')
    save_path = (args or {}).get('save_path', '')

    if not save_path:
        ts = time.strftime('%Y%m%d_%H%M%S')
        serial_safe = (device or 'default').replace(':', '_').replace('.', '_')
        save_path = rf'C:\MirageWork\MirageVulkan\screenshots\ss_{serial_safe}_{ts}.png'

    # Validate save_path against traversal so a user-supplied path can't escape
    # into arbitrary locations (mirror system.py:_validate_path).
    raw_parts = save_path.replace('\\', '/').split('/')
    if '..' in raw_parts:
        return {'error': f'save_path traversal denied (.. segment): {save_path!r}'}

    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    # ADB screencap
    adb_args = (['-s', device] if device else []) + \
               ['exec-out', 'screencap', '-p']
    cmd = [ADB_EXE] + adb_args
    try:
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        elapsed = time.perf_counter() - t0
        if r.returncode != 0:
            return {'error': r.stderr.decode(errors='replace')}
        with open(save_path, 'wb') as f:
            f.write(r.stdout)
        b64 = base64.b64encode(r.stdout).decode()
        return {
            'status': 'ok',
            'device': device,
            'path': save_path,
            'size_bytes': len(r.stdout),
            'timing': f'total={elapsed:.1f}s',
            'base64_png': b64[:500] + '...',  # 先頭のみ（サイズ節約）
        }
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# desktop_screenshot
# ---------------------------------------------------------------------------
def tool_desktop_screenshot(args: dict) -> dict:
    save_path = (args or {}).get('save_path', '')
    if not save_path:
        ts = time.strftime('%Y%m%d_%H%M%S')
        save_path = rf'C:\MirageWork\MirageVulkan\screenshots\desktop_{ts}.png'

    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    # PowerShell で Windows スクリーンショット
    ps_cmd = (
        f'Add-Type -AssemblyName System.Windows.Forms; '
        f'$bmp = [System.Drawing.Bitmap]::new('
        f'[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,'
        f'[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height); '
        f'$g = [System.Drawing.Graphics]::FromImage($bmp); '
        f'$g.CopyFromScreen(0,0,0,0,$bmp.Size); '
        f'$bmp.Save("{save_path}"); '
        f'$g.Dispose(); $bmp.Dispose()'
    )
    try:
        r = subprocess.run(
            ['powershell', '-Command', ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        if not os.path.exists(save_path):
            return {'error': r.stderr[:200]}
        size = os.path.getsize(save_path)
        with open(save_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
        return {
            'status': 'ok', 'type': 'desktop',
            'path': save_path, 'size_bytes': size,
            'base64_png': b64,
        }
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# usb_hub_control
# ---------------------------------------------------------------------------
def tool_usb_hub_control(args: dict) -> dict:
    """ReTRY HUB のポート電源制御。既存の usb_hub.py を呼ぶ。"""
    try:
        sys.path.insert(0, r'C:\MirageWork\mcp-server')
        from usb_hub import control_port
        port   = str((args or {}).get('port', ''))
        action = str((args or {}).get('action', 'status'))
        result = control_port(port, action)
        return result
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# wifi_adb_guard
# ---------------------------------------------------------------------------
def tool_wifi_adb_guard(args: dict) -> dict:
    device = (args or {}).get('device', '')
    model  = (args or {}).get('model', '')
    # 全デバイスのWiFi ADB疎通確認
    results = {}
    targets = [device] if device else [d['wifi'] for d in DEVICES.values()]
    for target in targets:
        ok, out, err = _adb(['-s', target, 'shell', 'echo', 'ok'], timeout=5)
        results[target] = 'connected' if ok and 'ok' in out else f'failed: {err}'
    return {'results': results, 'all_ok': all('connected' in v for v in results.values())}

# ---------------------------------------------------------------------------
# safe_reboot
# ---------------------------------------------------------------------------
def tool_safe_reboot(args: dict) -> dict:
    device       = (args or {}).get('device', '')
    timeout      = int((args or {}).get('timeout', 120) or 120)
    wifi_device  = (args or {}).get('wifi_device', device)

    if not device:
        return {'error': 'device required'}

    # 事前にWiFi ADB確認
    guard = tool_wifi_adb_guard({'device': wifi_device or device})
    if not guard.get('all_ok'):
        return {'error': f'WiFi ADB pre-check failed: {guard}'}

    # Reboot
    ok, out, err = _adb(['-s', device, 'reboot'], timeout=10)
    if not ok:
        return {'error': f'reboot failed: {err}'}

    # 再接続待ち
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        ok2, out2, _ = _adb(['-s', wifi_device or device, 'shell', 'echo', 'ok'], timeout=5)
        if ok2 and 'ok' in out2:
            # WiFi ADB再有効化
            _adb(['-s', wifi_device or device, 'shell', 'setprop', 'service.adb.tcp.port', '5555'])
            return {'ok': True, 'device': device, 'elapsed': round(time.time() - (deadline - timeout), 1)}

    return {'error': f'device did not reconnect within {timeout}s'}

# ---------------------------------------------------------------------------
# usb_recovery
# ---------------------------------------------------------------------------
def tool_usb_recovery(args: dict) -> dict:
    """既存の usb_recovery ロジックを呼ぶ（旧サーバーフォールバック）。"""
    try:
        sys.path.insert(0, r'C:\MirageWork\mcp-server')
        import server as old_server
        handler = old_server.TOOLS.get('usb_recovery', {}).get('handler')
        if handler:
            return handler(args)
        return {'error': 'usb_recovery handler not found in old server'}
    except Exception as e:
        return {'error': str(e)}

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# device_health - One-shot device health check
# ---------------------------------------------------------------------------
def tool_device_health(args: dict) -> dict:
    """Check device health in one call: WiFi ADB, TCP port reachability,
    APK running, battery level, screen state, RNDIS IP.

    Args:
        device:     WiFi ADB address (e.g. 192.168.0.10:5555)
        tcp_host:   USBLAN/WiFi IP for TCP port check
        tcp_port:   Video TCP port (e.g. 50000)
        apk_pkg:    APK package name (default: com.mirage.capture)
    """
    import socket as _socket
    import subprocess as _sub

    device   = (args or {}).get('device', '')
    tcp_host = (args or {}).get('tcp_host', '')
    tcp_port = int((args or {}).get('tcp_port', 0) or 0)
    apk_pkg  = (args or {}).get('apk_pkg', 'com.mirage.capture')
    adb_exe  = r'C:\Users\jun\AppData\Local\Android\Sdk\platform-tools\adb.exe'

    result = {
        'device': device,
        'wifi_adb': False,
        'tcp_reachable': None,
        'apk_running': False,
        'battery': None,
        'screen_on': None,
        'rndis_ip': None,
        'errors': [],
    }

    if not device:
        return {'error': 'device required (e.g. 192.168.0.10:5555)'}

    def _adb(cmd, timeout=5):
        try:
            r = _sub.run(
                [adb_exe, '-s', device, 'shell'] + cmd.split(),
                capture_output=True, text=True,
                timeout=timeout, encoding='utf-8', errors='replace',
            )
            return r.stdout.strip(), r.returncode == 0
        except Exception as e:
            return str(e), False

    # 1. WiFi ADB connectivity
    try:
        r = _sub.run(
            [adb_exe, 'connect', device],
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace',
        )
        result['wifi_adb'] = 'connected' in r.stdout.lower() or 'already' in r.stdout.lower()
    except Exception as e:
        result['errors'].append(f'adb_connect: {e}')

    if result['wifi_adb']:
        # 2. Battery
        out, ok = _adb('dumpsys battery')
        if ok:
            for line in out.split('\n'):
                if 'level:' in line.lower():
                    try:
                        result['battery'] = int(line.split(':')[1].strip())
                    except Exception:
                        pass
                    break

        # 3. Screen state
        out, ok = _adb('dumpsys power')
        if ok:
            result['screen_on'] = 'mWakefulness=Awake' in out or 'Display Power: state=ON' in out

        # 4. APK running
        out, ok = _adb(f'pidof {apk_pkg}')
        result['apk_running'] = ok and bool(out.strip())

        # 5. RNDIS IP (rndis0 or usb0)
        for iface in ('rndis0', 'usb0', 'rndis1'):
            out, ok = _adb(f'ip addr show {iface}')
            if ok and 'inet ' in out:
                import re as _re
                m = _re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
                if m:
                    result['rndis_ip'] = m.group(1)
                    result['rndis_iface'] = iface
                    break

    # 6. TCP port reachability (independent of ADB)
    if tcp_host and tcp_port:
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(2)
            r = sock.connect_ex((tcp_host, tcp_port))
            sock.close()
            result['tcp_reachable'] = (r == 0)
        except Exception as e:
            result['tcp_reachable'] = False
            result['errors'].append(f'tcp: {e}')

    # Overall health score
    checks = [result['wifi_adb'], result['apk_running']]
    if result['tcp_reachable'] is not None:
        checks.append(result['tcp_reachable'])
    passed = sum(1 for c in checks if c)
    result['health'] = 'GREEN' if passed == len(checks) else \
                       'YELLOW' if passed > 0 else 'RED'
    result['health_score'] = f'{passed}/{len(checks)}'

    return result


TOOLS = {
    'device_health': {
        'description': 'One-shot device health check: WiFi ADB + TCP port + APK running + battery + screen + RNDIS IP. Returns health=GREEN/YELLOW/RED.',
        'schema': {'type': 'object', 'properties': {
            'device':   {'type': 'string', 'description': 'WiFi ADB address (e.g. 192.168.0.10:5555)'},
            'tcp_host': {'type': 'string', 'description': 'IP for TCP port check'},
            'tcp_port': {'type': 'integer', 'description': 'TCP video port (e.g. 50000)'},
            'apk_pkg':  {'type': 'string',  'description': 'APK package (default: com.mirage.capture)'},
        }, 'required': ['device']},
        'handler': tool_device_health,
    },
    'adb_devices': {
        'description': 'List connected Android devices.',
        'schema': {'type': 'object', 'properties': {}},
        'handler': tool_adb_devices,
    },
    'adb_shell': {
        'description': 'Run ADB shell command on device.',
        'schema': {'type': 'object', 'properties': {
            'device': {'type': 'string'},
            'command': {'type': 'string'},
            'timeout': {'type': 'integer'},
        }, 'required': ['command']},
        'handler': tool_adb_shell,
    },
    'screenshot': {
        'description': 'Take screenshot from Android device.',
        'schema': {'type': 'object', 'properties': {
            'device': {'type': 'string'},
            'save_path': {'type': 'string'},
        }},
        'handler': tool_screenshot,
    },
    'desktop_screenshot': {
        'description': 'Take screenshot of Windows desktop.',
        'schema': {'type': 'object', 'properties': {
            'save_path': {'type': 'string'},
        }},
        'handler': tool_desktop_screenshot,
    },
    'usb_hub_control': {
        'description': 'ReTRY HUB USB port power control (on/off/cycle/status).',
        'schema': {'type': 'object', 'properties': {
            'port': {'type': 'string'},
            'action': {'type': 'string', 'enum': ['on', 'off', 'cycle', 'status']},
        }, 'required': ['port']},
        'handler': tool_usb_hub_control,
    },
    'wifi_adb_guard': {
        'description': 'WiFi ADB pre-flight check.',
        'schema': {'type': 'object', 'properties': {
            'device': {'type': 'string'},
            'model': {'type': 'string'},
        }},
        'handler': tool_wifi_adb_guard,
    },
    'safe_reboot': {
        'description': 'WiFi ADB-guarded device reboot.',
        'schema': {'type': 'object', 'properties': {
            'device': {'type': 'string'},
            'timeout': {'type': 'integer'},
            'wifi_device': {'type': 'string'},
        }, 'required': ['device']},
        'handler': tool_safe_reboot,
    },
    'usb_recovery': {
        'description': 'Full USB recovery: power cycle hub port, wait for device reconnect.',
        'schema': {'type': 'object', 'properties': {
            'port': {'type': 'string'},
            'device': {'type': 'string'},
            'wifi_device': {'type': 'string'},
            'wait_timeout': {'type': 'integer'},
        }, 'required': ['port']},
        'handler': tool_usb_recovery,
    },
}
