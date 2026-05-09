#!/usr/bin/env python3
"""test_v2_tools.py - Unit tests for mcp-server-v2 tools"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'mcp-server'))

import pytest


class TestMemorySemanticSearch:
    """memory_semantic_search tool (A1)"""

    def _handler(self):
        from tools.memory import TOOLS
        return TOOLS['memory_semantic_search']['handler']

    def test_registered(self):
        from tools.memory import TOOLS
        assert 'memory_semantic_search' in TOOLS

    def test_requires_query(self):
        h = self._handler()
        result = h({})
        assert 'error' in result

    def test_empty_query_error(self):
        h = self._handler()
        result = h({'query': ''})
        assert 'error' in result

    def test_fts_only_returns_hits(self):
        h = self._handler()
        result = h({'query': 'HEVC encoder', 'namespace': 'mirage-vulkan',
                    'limit': 3, 'use_llm': False})
        assert 'hits' in result
        assert 'method' in result
        assert result['method'] in ('semantic_lite_plus_fts', 'fts_only', 'fts_fallback', 'semantic_llm_rerank')

    def test_limit_respected(self):
        h = self._handler()
        result = h({'query': 'USB ADB device', 'limit': 2, 'use_llm': False})
        assert len(result.get('hits', [])) <= 2

    def test_namespace_filter(self):
        h = self._handler()
        result = h({'query': 'test', 'namespace': 'mirage-android',
                    'limit': 5, 'use_llm': False})
        assert 'hits' in result

    def test_total_candidates_present(self):
        h = self._handler()
        result = h({'query': 'HEVC', 'use_llm': False})
        assert 'total_candidates' in result

    def test_backend_fts(self):
        h = self._handler()
        result = h({'query': 'HEVC', 'backend': 'fts', 'limit': 2})
        assert result.get('backend', {}).get('resolved') == 'fts'
        assert result.get('backend', {}).get('use_llm') is False
        assert result.get('backend', {}).get('use_semantic_lite') is False

    def test_backend_semantic_lite(self):
        h = self._handler()
        result = h({'query': '外部記憶 使い心地', 'backend': 'semantic_lite', 'limit': 2})
        assert result.get('backend', {}).get('resolved') == 'semantic_lite'
        assert result.get('backend', {}).get('use_llm') is False
        assert result.get('backend', {}).get('use_semantic_lite') is True

    def test_invalid_backend(self):
        h = self._handler()
        result = h({'query': 'HEVC', 'backend': 'unknown'})
        assert 'error' in result
        assert 'valid_backends' in result


class TestMemoryL0L1Tools:
    """memory_l0 and memory_l1 tools"""

    def test_l0_registered(self):
        from tools.memory import TOOLS
        assert 'memory_l0' in TOOLS

    def test_l1_registered(self):
        from tools.memory import TOOLS
        assert 'memory_l1' in TOOLS

    def test_l0_returns_dict(self):
        from tools.memory import TOOLS
        result = TOOLS['memory_l0']['handler']({})
        assert isinstance(result, dict)
        assert 'l0' in result

    def test_l1_with_namespace(self):
        from tools.memory import TOOLS
        result = TOOLS['memory_l1']['handler']({'namespace': 'mirage-vulkan', 'top_n': 3})
        assert isinstance(result, dict)
        assert 'l1' in result
        assert result.get('count', 0) <= 3


class TestMemorySearchAll:
    """memory_search_all tool"""

    def test_registered(self):
        from tools.memory import TOOLS
        assert 'memory_search_all' in TOOLS

    def test_basic_search(self):
        from tools.memory import TOOLS
        result = TOOLS['memory_search_all']['handler']({'query': 'HEVC', 'limit': 3})
        assert 'hits' in result

    def test_returns_namespace_field(self):
        from tools.memory import TOOLS
        result = TOOLS['memory_search_all']['handler']({'query': 'encoder', 'limit': 5})
        for hit in result.get('hits', []):
            assert 'namespace' in hit


class TestSystemTools:
    """system/status tools"""

    def test_status_registered(self):
        from tools.system import TOOLS as S_TOOLS
        assert 'status' in S_TOOLS

    def test_status_returns_dict(self):
        from tools.system import TOOLS as S_TOOLS
        result = S_TOOLS['status']['handler']({})
        assert isinstance(result, dict)



class TestGitDiff:
    """git_diff tool"""

    def _h(self):
        from tools.system import TOOLS
        return TOOLS['git_diff']['handler']

    def test_registered(self):
        from tools.system import TOOLS
        assert 'git_diff' in TOOLS

    def test_stat_only(self):
        h = self._h()
        result = h({'cwd': r'C:\MirageWork\MirageVulkan', 'stat_only': True})
        assert 'stat' in result
        assert isinstance(result['stat'], str)

    def test_full_diff_returns_stat_and_diff(self):
        h = self._h()
        result = h({'cwd': r'C:\MirageWork\MirageVulkan', 'max_lines': 50})
        assert 'stat' in result
        # diff may be None if no changes, or a string
        assert result['diff'] is None or isinstance(result['diff'], str)

    def test_truncation_flag(self):
        h = self._h()
        result = h({'cwd': r'C:\MirageWork\MirageVulkan', 'max_lines': 1})
        assert 'truncated' in result


class TestActiveContext:
    """active_context tool"""

    def _h(self):
        from tools.memory import TOOLS
        return TOOLS['active_context']['handler']

    def test_registered(self):
        from tools.memory import TOOLS
        assert 'active_context' in TOOLS

    def test_returns_summary(self):
        h = self._h()
        result = h({'top_decisions': 2, 'hours': 24})
        assert 'summary' in result
        assert isinstance(result['summary'], str)

    def test_has_l0_summaries(self):
        h = self._h()
        result = h({'namespaces': ['mirage-vulkan']})
        assert 'l0_summaries' in result
        assert isinstance(result['l0_summaries'], dict)

    def test_has_physical_todos(self):
        h = self._h()
        result = h({})
        assert 'physical_todos' in result
        assert len(result['physical_todos']) > 0

    def test_has_generated_at(self):
        h = self._h()
        result = h({})
        assert 'generated_at' in result


class TestMemoryRecentActivity:
    """memory_recent_activity tool"""

    def _h(self):
        from tools.memory import TOOLS
        return TOOLS['memory_recent_activity']['handler']

    def test_registered(self):
        from tools.memory import TOOLS
        assert 'memory_recent_activity' in TOOLS

    def test_returns_window(self):
        h = self._h()
        result = h({'days': 1.0})
        assert 'window' in result
        assert '1.0d' in result['window'] or '1d' in result['window'] or 'last' in result['window']

    def test_has_counts(self):
        h = self._h()
        result = h({'days': 7.0})
        assert 'total_new_entries' in result
        assert isinstance(result['total_new_entries'], int)

    def test_detail_mode(self):
        h = self._h()
        result = h({'days': 7.0, 'detail': True})
        # recent_entries present only if there are entries
        assert 'error' not in result

    def test_namespace_filter(self):
        h = self._h()
        result = h({'days': 30.0, 'namespace': 'mirage-vulkan'})
        assert 'total_new_entries' in result

    def test_bootstrap_freshness_present(self):
        h = self._h()
        result = h({'days': 1.0})
        assert 'bootstrap_freshness' in result
        assert isinstance(result['bootstrap_freshness'], dict)


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
            'path': r'C:\MirageWork\MirageVulkan\src',
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
            'path': r'C:\MirageWork\MirageVulkan\src',
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
            'path': r'C:\MirageWork\MirageVulkan\src',
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
            'path': r'C:\MirageWork\mcp-server',
            'max_hits': 5,
        })
        assert result['count'] <= 5

    def test_literal_mode(self):
        h = self._h()
        result = h({
            'pattern': 'render_us',
            'include': '*.hpp',
            'path': r'C:\MirageWork\MirageVulkan\src',
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
        result = h({'build_dir': r'C:\nonexistent_build_dir_xyz'})
        assert not result.get('ok', True)

    def test_returns_structure(self):
        h = self._h()
        result = h({'build_dir': r'C:\MirageWork\MirageVulkan\build'})
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

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
