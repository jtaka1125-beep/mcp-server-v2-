#!/usr/bin/env python3
"""patch_test_v2_new.py - Add tests for 4 new tools"""

path = r'C:\MirageWork\mcp-server-v2\test_v2_tools.py'
content = open(path, 'r', encoding='utf-8').read()

new_tests = '''

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
        result = h({'cwd': r'C:\\MirageWork\\MirageVulkan', 'stat_only': True})
        assert 'stat' in result
        assert isinstance(result['stat'], str)

    def test_full_diff_returns_stat_and_diff(self):
        h = self._h()
        result = h({'cwd': r'C:\\MirageWork\\MirageVulkan', 'max_lines': 50})
        assert 'stat' in result
        # diff may be None if no changes, or a string
        assert result['diff'] is None or isinstance(result['diff'], str)

    def test_truncation_flag(self):
        h = self._h()
        result = h({'cwd': r'C:\\MirageWork\\MirageVulkan', 'max_lines': 1})
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
'''

# Add before if __name__
marker = "\nif __name__ == \"__main__\":"
if marker in content:
    content = content.replace(marker, new_tests + marker)
else:
    content += new_tests

open(path, 'w', encoding='utf-8').write(content)
print('Tests added')
