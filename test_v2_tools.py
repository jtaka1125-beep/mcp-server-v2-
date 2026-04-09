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
        assert result['method'] in ('fts_only', 'fts_fallback', 'semantic_llm_rerank')

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

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
