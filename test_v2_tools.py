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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
