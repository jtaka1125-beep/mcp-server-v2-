"""
tools/pipeline.py - パイプライン系ツール
==========================================
task_queue.py の Pipeline を呼び出すwrapper。
"""
import os
import sys
import uuid
import time
import json
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, r'C:\MirageWork\mcp-server')

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# run_pipeline
# ---------------------------------------------------------------------------
def tool_run_pipeline(args: dict) -> str:
    try:
        from server import tool_run_pipeline as _orig
        return _orig(args)
    except Exception as e:
        return f'ERROR: {e}'

# ---------------------------------------------------------------------------
# pipeline_status
# ---------------------------------------------------------------------------
def tool_pipeline_status(args: dict) -> str:
    try:
        from server import tool_pipeline_status as _orig
        return _orig(args)
    except Exception as e:
        return f'ERROR: {e}'

# ---------------------------------------------------------------------------
# pipeline_cancel
# ---------------------------------------------------------------------------
def tool_pipeline_cancel(args: dict) -> str:
    try:
        from server import tool_pipeline_cancel as _orig
        return _orig(args)
    except Exception as e:
        return f'ERROR: {e}'

# ---------------------------------------------------------------------------
# pipeline_resume
# ---------------------------------------------------------------------------
def tool_pipeline_resume(args: dict) -> str:
    try:
        from server import tool_pipeline_resume as _orig
        return _orig(args)
    except Exception as e:
        return f'ERROR: {e}'

# ---------------------------------------------------------------------------
# queue_create_and_wait
# ---------------------------------------------------------------------------
def tool_queue_create_and_wait(args: dict) -> str:
    try:
        from server import tool_queue_create_and_wait as _orig
        return _orig(args)
    except Exception as e:
        return f'ERROR: {e}'

# ---------------------------------------------------------------------------
# ツール登録テーブル
# ---------------------------------------------------------------------------
TOOLS = {
    'run_pipeline': {
        'description': 'Auto-split large task into subtasks and execute sequentially with retry support.',
        'schema': {'type': 'object', 'properties': {
            'prompt':   {'type': 'string'},
            'parallel': {'type': 'boolean'},
            'model':    {'type': 'string'},
            'wait_next':{'type': 'boolean'},
        }, 'required': ['prompt']},
        'handler': tool_run_pipeline,
    },
    'pipeline_status': {
        'description': 'Check pipeline progress.',
        'schema': {'type': 'object', 'properties': {
            'pipeline_id': {'type': 'string'},
        }},
        'handler': tool_pipeline_status,
    },
    'pipeline_cancel': {
        'description': 'Cancel a running pipeline.',
        'schema': {'type': 'object', 'properties': {
            'pipeline_id': {'type': 'string'},
        }, 'required': ['pipeline_id']},
        'handler': tool_pipeline_cancel,
    },
    'pipeline_resume': {
        'description': 'Resume a suspended pipeline from where it stopped.',
        'schema': {'type': 'object', 'properties': {
            'pipeline_id': {'type': 'string'},
        }, 'required': ['pipeline_id']},
        'handler': tool_pipeline_resume,
    },
    'queue_create_and_wait': {
        'description': 'Create queue for external signal and wait.',
        'schema': {'type': 'object', 'properties': {
            'queue_id': {'type': 'string'},
            'timeout':  {'type': 'integer'},
        }},
        'handler': tool_queue_create_and_wait,
    },
}
