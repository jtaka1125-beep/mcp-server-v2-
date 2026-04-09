"""
memory/store.py - 既存memory_store.pyの薄いwrapper
====================================================
DBは既存の memory.db を共有する。
"""
import sys, os
sys.path.insert(0, r'C:\MirageWork\mcp-server')

from memory_store import (
    append_entry,
    get_bootstrap,
    search,
    fetch_recent_raw,
    compact_update_bootstrap,
    compact_store_extracted,
)

__all__ = [
    'append_entry',
    'get_bootstrap',
    'search',
    'fetch_recent_raw',
    'compact_update_bootstrap',
    'compact_store_extracted',
]
