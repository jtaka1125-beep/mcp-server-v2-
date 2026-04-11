#!/usr/bin/env python3
"""patch_v2_routes.py - add missing /api/v1/* routes to V2 server"""

path = r'C:\MirageWork\mcp-server-v2\server.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

NEW_ROUTES = """            elif section == 'adb':
                import tools.device as dev_tools
                if action == 'devices':
                    result = dev_tools.TOOLS['adb_devices']['handler']({})
                    self._send_json(200, result)
                else:
                    cmd = qs.get('cmd', [''])[0]
                    device = qs.get('device', [''])[0]
                    result = dev_tools.TOOLS['adb_shell']['handler']({'command': cmd, 'device': device})
                    self._send_json(200, result)
            elif section == 'git':
                import tools.system as sys_tools2
                result = sys_tools2.TOOLS['git_status']['handler']({})
                self._send_json(200, result)
            elif section == 'exec':
                import urllib.parse as _up
                cmd = _up.unquote_plus(qs.get('cmd', [''])[0])
                import tools.system as sys_tools3
                result = sys_tools3.TOOLS['run_command']['handler']({'command': cmd})
                self._send_json(200, result)
            elif section == 'read':
                file_path = qs.get('path', [''])[0]
                import tools.system as sys_tools4
                result = sys_tools4.TOOLS['read_file']['handler']({'path': file_path})
                self._send_json(200, result)
            elif section == 'list':
                file_path = qs.get('path', [''])[0]
                import tools.system as sys_tools5
                result = sys_tools5.TOOLS['list_files']['handler']({'path': file_path})
                self._send_json(200, result)
"""

OLD = "            else:\n                self._send_json(404, {'error': f'unknown section: {section}'})"
NEW = NEW_ROUTES + OLD

if OLD in content:
    content = content.replace(OLD, NEW, 1)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print('OK: routes added')
else:
    print('ERROR: marker not found')
    # show what's there
    idx = content.find("url_queue")
    print(repr(content[idx:idx+200]))
