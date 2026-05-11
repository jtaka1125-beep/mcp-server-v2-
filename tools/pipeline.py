"""pipeline.py - no-op stubs

The original pipeline tools (run_pipeline / pipeline_status / pipeline_cancel /
pipeline_resume / queue_create_and_wait) wrapped legacy V1 functions that were
deleted with the V1 slim refactor; commit a4a530a removed the broken wrappers.

This file restores tools at the same names purely so callers that probe these
tool names (notably the in-process loop engine that polls pipeline_status for
progress) get a defined response instead of ImportError. They are intentionally
no-ops; for real pipeline-style execution use run_loop_v2.
"""

def _stub_status(_args: dict) -> dict:
    return {
        'pipelines': [],
        'note': 'pipeline tools are stubbed (legacy V1 dependency removed in a4a530a). '
                'Use run_loop_v2 for multi-step task execution.',
        'deprecated': True,
    }


def _stub_unsupported(_args: dict) -> dict:
    return {
        'ok': False,
        'error': 'pipeline tools are deprecated; use run_loop_v2 instead',
        'deprecated': True,
    }


TOOLS = {
    'run_pipeline': {
        'description': 'DEPRECATED stub. Use run_loop_v2.',
        'schema': {'type': 'object', 'properties': {'prompt': {'type': 'string'}}, 'required': ['prompt']},
        'handler': _stub_unsupported,
    },
    'pipeline_status': {
        'description': 'No-op stub returning empty pipelines list (deprecated).',
        'schema': {'type': 'object', 'properties': {'pipeline_id': {'type': 'string'}}},
        'handler': _stub_status,
    },
    'pipeline_cancel': {
        'description': 'DEPRECATED stub.',
        'schema': {'type': 'object', 'properties': {'pipeline_id': {'type': 'string'}}, 'required': ['pipeline_id']},
        'handler': _stub_unsupported,
    },
    'pipeline_resume': {
        'description': 'DEPRECATED stub.',
        'schema': {'type': 'object', 'properties': {'pipeline_id': {'type': 'string'}}, 'required': ['pipeline_id']},
        'handler': _stub_unsupported,
    },
    'queue_create_and_wait': {
        'description': 'DEPRECATED stub.',
        'schema': {'type': 'object', 'properties': {}},
        'handler': _stub_unsupported,
    },
}
