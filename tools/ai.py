"""ai_context: Self-Describing System Interface for MirageSystem.

Returns a single JSON aggregating system / memory / devices / capabilities /
interfaces / runtime / iron_laws. Designed to be pulled by any AI client
(Claude Code, GPT, etc.) so they share the same MirageSystem mental model.

Design principles (2026-04-28, Jun + Code + GPT collaboration):
- facts only (no recommendations / inference; those go in a separate endpoint)
- system (static) / runtime (dynamic) split for caching strategies
- memory layer is a first-class component, not a hidden tool
- iron_laws are exposed as machine-readable constraints
- primary_interaction: 'mcp_tools' to communicate that REST is secondary
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List


IRON_LAWS: List[str] = [
    "APK uninstall 禁止（Device Owner 消滅）",
    "USB デバッグ無効化禁止",
    "H264 使用禁止（HEVC のみ）",
    "AOA 処理禁止（TCP/RNDIS のみ）",
    "D3D11VA 使用禁止（AMD iGPU バグ）",
    "V2 アーキテクチャを破壊しない（実体は mcp-server-v2/tools/*、"
    "archive_2026-04-25/server_legacy.py を復活させない）",
]

CAPABILITIES: Dict[str, bool] = {
    "adb_control": True,
    "multi_device": True,
    "screenshot": True,
    "build_system": True,
    "task_execution": True,
    "pipeline_execution": True,
    "memory_query": True,
    "memory_link_traverse": True,
}

DB_PATH = r"C:\MirageWork\mcp-server\data\memory.db"


def _build_memory_block() -> Dict[str, Any]:
    """Aggregate memory.db live stats into the ai_context payload."""
    out: Dict[str, Any] = {
        "primary_store": "memory.db",
        "type": "sqlite",
        "features": {
            "full_text_search": "FTS5 unicode61",
            "graph_links": True,
            "auto_id_mention_links": True,
            "supersede_chain": True,
            "constitution_layer": True,
        },
    }
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            entries = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            links = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            ns_rows = con.execute(
                "SELECT DISTINCT namespace FROM entries ORDER BY namespace"
            ).fetchall()
            namespaces = [r["namespace"] for r in ns_rows]

            now = int(time.time())
            boot_rows = con.execute(
                "SELECT namespace, updated_at FROM bootstrap"
            ).fetchall()
            freshness = {
                r["namespace"]: int((now - int(r["updated_at"] or 0)) / 3600)
                for r in boot_rows
            }

            stale_ts = now - 30 * 86400
            stale = con.execute(
                "SELECT COUNT(*) FROM entries WHERE type='decision' "
                "AND created_at < ? "
                "AND id NOT IN (SELECT source_id FROM links WHERE relation_type='supersedes') "
                "AND (status IS NULL OR status='active')",
                (stale_ts,),
            ).fetchone()[0]
            contrad = con.execute(
                "SELECT COUNT(*) FROM links WHERE relation_type='contradicts'"
            ).fetchone()[0]

            out.update({
                "entries": int(entries),
                "links": int(links),
                "namespaces": namespaces,
                "bootstrap_freshness_hours": freshness,
                "lint_summary": {
                    "stale_decisions": int(stale),
                    "contradictions": int(contrad),
                },
            })
        finally:
            con.close()
    except Exception as e:
        out["error"] = f"memory.db query failed: {e}"
    return out


def _build_system_block() -> Dict[str, Any]:
    """Static + lightweight dynamic facts about the MCP server itself."""
    block: Dict[str, Any] = {
        "name": "MirageSystem",
        "version": "5.0",
        "components": {
            "mcp_v1": {"port": 3000, "role": "slim proxy"},
            "mcp_v2": {"port": 3001, "role": "full tool surface"},
            "watchdog": {"type": "schtask", "name": "mcp_guard_v2.ps1"},
        },
    }
    try:
        import psutil
        for pname, port in (("mcp_v1", 3000), ("mcp_v2", 3001)):
            for c in psutil.net_connections(kind="inet"):
                if c.status == "LISTEN" and c.laddr and c.laddr.port == port:
                    block["components"][pname]["pid"] = c.pid
                    block["components"][pname]["status"] = "running"
                    break
            else:
                block["components"][pname]["status"] = "down"
    except Exception:
        # psutil optional; leave status unknown
        pass
    return block


def _parse_device_info(info: str) -> Dict[str, str]:
    """Parse adb-style 'product:X model:Y device:Z transport_id:N' string."""
    out: Dict[str, str] = {}
    for tok in (info or "").split():
        if ":" in tok:
            k, _, v = tok.partition(":")
            out[k] = v
    return out


def _build_devices_block() -> List[Dict[str, Any]]:
    """Device list from adb_devices. One entry per serial, online/offline based
    on adb state. Name inferred from product/model in the info string.
    """
    devices: List[Dict[str, Any]] = []
    try:
        from tools import device as dev_tools
        result = dev_tools.TOOLS["adb_devices"]["handler"]({}) or {}
        raw_list = result.get("devices") or []
        if isinstance(raw_list, list):
            for d in raw_list:
                if not isinstance(d, dict):
                    continue
                serial = d.get("serial") or d.get("id") or ""
                state = d.get("state") or "unknown"
                info = _parse_device_info(d.get("info") or "")
                product = info.get("product") or info.get("model") or ""
                devices.append({
                    "id": serial,
                    "name": product,
                    "transport": "tcp" if ":5555" in serial else (
                        "tls" if "_adb-tls-connect" in serial else "usb"
                    ),
                    "status": "online" if state == "device" else state,
                })
    except Exception as e:
        devices.append({"error": f"adb_devices failed: {e}"})
    return devices


def _build_interfaces_block() -> Dict[str, Any]:
    """Static + tool-registry-derived interface description."""
    block: Dict[str, Any] = {
        "primary_interaction": "mcp_tools",
        "api_base": "/api/v1",
    }
    try:
        # Walk the registered TOOLS dict (lives in server.py / __main__).
        # Lookup via sys.modules to avoid circular imports.
        import sys
        srv = sys.modules.get("server") or sys.modules.get("__main__")
        tool_names = list(getattr(srv, "TOOLS", {}).keys()) if srv else []
        block["mcp_tools_count"] = len(tool_names)
        # Derive prefixes (memory_*, adb_*, etc.) for compact summary.
        prefixes: Dict[str, int] = {}
        for n in tool_names:
            head = n.split("_", 1)[0]
            prefixes[head] = prefixes.get(head, 0) + 1
        block["mcp_tool_categories"] = sorted(
            [f"{k}_*" for k in prefixes.keys()]
        )
    except Exception as e:
        block["error"] = f"tool registry walk failed: {e}"
    return block


def _build_runtime_block() -> Dict[str, Any]:
    """Best-effort runtime facts. Missing pieces just degrade gracefully."""
    block: Dict[str, Any] = {
        "active_tasks": 0,
        "active_pipelines": 0,
    }
    # Future: pull from task / pipeline tools when they expose a query API.
    # Currently kept minimal to avoid stale data.
    return block


def tool_ai_context(args: dict) -> dict:
    """Return the Self-Describing System Interface JSON.

    Pull-only, facts-only. AI clients call this once at session start (or
    on demand) to align their mental model with reality.
    """
    return {
        "system": _build_system_block(),
        "memory": _build_memory_block(),
        "devices": _build_devices_block(),
        "capabilities": CAPABILITIES,
        "interfaces": _build_interfaces_block(),
        "runtime": _build_runtime_block(),
        "iron_laws": IRON_LAWS,
        "generated_at": int(time.time()),
    }


TOOLS: Dict[str, Any] = {
    "ai_context": {
        "description": (
            "Self-Describing System Interface. Returns one JSON aggregating "
            "system / memory / devices / capabilities / interfaces / runtime "
            "/ iron_laws so AI clients (Claude Code, GPT, etc.) share the "
            "same mental model of MirageSystem. facts only, no recommendations."
        ),
        "schema": {"type": "object", "properties": {}},
        "handler": tool_ai_context,
    },
}
