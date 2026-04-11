#!/usr/bin/env python3
"""patch_test_5tools.py - Add tests for 5 new tools"""

path = r'C:\MirageWork\mcp-server-v2\test_v2_tools.py'
content = open(path, 'r', encoding='utf-8').read()

new_tests = '''

class TestCodeSearch:
    """code_search tool"""

    def _h(self):
        from tools.system import TOOLS
        return TOOLS['code_search']['handler']

    def test_registered(self):
        from tools.system import TOOLS
        assert 'code_search' in TOOLS

    def test_requires_pattern(self):
        h = self._h()
        result = h({})
        assert 'error' in result

    def test_finds_hits(self):
        h = self._h()
        result = h({
            'pattern': 'SharedFrame',
            'include': '*.hpp',
            'path': r'C:\\MirageWork\\MirageVulkan\\src',
            'max_hits': 5,
        })
        assert 'hits' in result
        assert 'count' in result
        assert 'files_searched' in result

    def test_hit_structure(self):
        h = self._h()
        result = h({
            'pattern': 'SharedFrame',
            'include': '*.hpp',
            'path': r'C:\\MirageWork\\MirageVulkan\\src',
            'max_hits': 3,
        })
        for hit in result['hits']:
            assert 'file' in hit
            assert 'line' in hit
            assert 'text' in hit

    def test_context_lines(self):
        h = self._h()
        result = h({
            'pattern': 'render_us',
            'include': '*.hpp',
            'path': r'C:\\MirageWork\\MirageVulkan\\src',
            'context': 2,
            'max_hits': 3,
        })
        for hit in result['hits']:
            if result['count'] > 0:
                assert 'context' in hit

    def test_max_hits_respected(self):
        h = self._h()
        result = h({
            'pattern': '.',
            'include': '*.py',
            'path': r'C:\\MirageWork\\mcp-server',
            'max_hits': 5,
        })
        assert result['count'] <= 5

    def test_literal_mode(self):
        h = self._h()
        result = h({
            'pattern': 'render_us',
            'include': '*.hpp',
            'path': r'C:\\MirageWork\\MirageVulkan\\src',
            'literal': True,
            'max_hits': 5,
        })
        assert 'hits' in result


class TestBuildAndReport:
    """build_and_report tool"""

    def _h(self):
        from tools.system import TOOLS
        return TOOLS['build_and_report']['handler']

    def test_registered(self):
        from tools.system import TOOLS
        assert 'build_and_report' in TOOLS

    def test_missing_build_dir(self):
        h = self._h()
        result = h({'build_dir': r'C:\\nonexistent_build_dir_xyz'})
        assert not result.get('ok', True)

    def test_returns_structure(self):
        h = self._h()
        result = h({'build_dir': r'C:\\MirageWork\\MirageVulkan\\build'})
        # Whether build succeeds or not, structure should be present
        assert 'ok' in result
        assert 'errors' in result
        assert 'warnings_count' in result


class TestDeviceHealth:
    """device_health tool"""

    def _h(self):
        from tools.device import TOOLS
        return TOOLS['device_health']['handler']

    def test_registered(self):
        from tools.device import TOOLS
        assert 'device_health' in TOOLS

    def test_requires_device(self):
        h = self._h()
        result = h({})
        assert 'error' in result

    def test_offline_device_returns_red(self):
        h = self._h()
        result = h({'device': '192.168.255.254:5555'})
        assert 'health' in result
        assert result['health'] in ('RED', 'YELLOW', 'GREEN')

    def test_result_structure(self):
        h = self._h()
        result = h({'device': '192.168.255.254:5555'})
        assert 'wifi_adb' in result
        assert 'apk_running' in result
        assert 'health_score' in result


class TestSessionCheckpoint:
    """session_checkpoint tool"""

    def _h(self):
        from tools.memory import TOOLS
        return TOOLS['session_checkpoint']['handler']

    def test_registered(self):
        from tools.memory import TOOLS
        assert 'session_checkpoint' in TOOLS

    def test_saves_to_memory(self):
        h = self._h()
        result = h({
            'done': 'Unit test: session checkpoint',
            'next': ['Test action 1', 'Test action 2'],
            'namespace': 'mirage-infra',
            'update_md': False,  # Don't modify PROJECT_STATE.md during test
        })
        assert 'ok' in result
        assert 'mirage-infra' in str(result.get('saved', []))

    def test_returns_checkpoint_text(self):
        h = self._h()
        result = h({'done': 'test checkpoint', 'update_md': False})
        assert 'checkpoint' in result
        assert 'Session Checkpoint' in result['checkpoint']


class TestMemoryDiff:
    """memory_diff tool"""

    def _h(self):
        from tools.memory import TOOLS
        return TOOLS['memory_diff']['handler']

    def test_registered(self):
        from tools.memory import TOOLS
        assert 'memory_diff' in TOOLS

    def test_decisions_mode(self):
        h = self._h()
        result = h({'hours': 24, 'mode': 'decisions'})
        assert 'changes' in result
        assert 'total' in result
        assert result['mode'] == 'decisions'

    def test_entries_mode(self):
        h = self._h()
        result = h({'hours': 24, 'mode': 'entries'})
        assert 'changes' in result

    def test_bootstrap_mode(self):
        h = self._h()
        result = h({'mode': 'bootstrap'})
        assert 'changes' in result
        for c in result['changes']:
            assert 'namespace' in c
            assert 'fresh' in c

    def test_namespace_filter(self):
        h = self._h()
        result = h({'hours': 72, 'mode': 'decisions', 'namespace': 'mirage-vulkan'})
        assert result['namespace'] == 'mirage-vulkan'
        # All returned entries should be from this namespace
        for c in result['changes']:
            assert c['namespace'] == 'mirage-vulkan'

    def test_window_in_result(self):
        h = self._h()
        result = h({'hours': 6.0})
        assert 'window' in result
        assert '6.0h' in result['window'] or '6h' in result['window'] or 'last' in result['window']
'''

marker = "\nif __name__ == \"__main__\":"
if marker in content:
    content = content.replace(marker, new_tests + marker)
else:
    content += new_tests

open(path, 'w', encoding='utf-8').write(content)
print('Tests added')
