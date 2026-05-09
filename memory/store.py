"""
memory/store.py - 既存memory_store.pyの薄いwrapper
====================================================
DBは既存の memory.db を共有する。
"""
import sys, os
sys.path.insert(0, r'C:\MirageWork\mirage-shared')

from memory_store import (
    append_entry,
    get_bootstrap,
    search,
    search_all,
    fetch_recent_raw,
    compact_update_bootstrap,
    compact_store_extracted,
    get_l0,
    get_l1,
    touch_entry,
    salience_score,
    semantic_lite_rebuild,
    semantic_lite_search,
    semantic_lite_status,
)

__all__ = [
    'append_entry',
    'get_bootstrap',
    'search',
    'search_all',
    'fetch_recent_raw',
    'compact_update_bootstrap',
    'compact_store_extracted',
    'get_l0',
    'get_l1',
    'touch_entry',
    'salience_score',
    'semantic_lite_rebuild',
    'semantic_lite_search',
    'semantic_lite_status',
]
