"""Launch the packaged stdio server and verify real MCP discovery/tool calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters, stdio_client


async def run(root: Path, trace_path: str) -> dict[str, Any]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "unirobosim_mcp.cli", "--root", str(root)],
    )
    async with Client(stdio_client(parameters)) as client:
        tools = await client.list_tools()
        names = sorted(tool.name for tool in tools.tools)
        summary = await client.call_tool("summarize_debug_trace", {"trace_path": trace_path})
        primitives = await client.call_tool(
            "query_debug_primitives",
            {"trace_path": trace_path, "sequence": None, "limit": 100},
        )
        reports = await client.call_tool(
            "query_debug_reports",
            {"trace_path": trace_path, "limit": 10},
        )
        rejected = await client.call_tool("read_debug_evidence", {"relative_path": "../outside"})
    expected = {
        "evidence_server_info",
        "list_debug_evidence",
        "query_debug_events",
        "query_debug_primitives",
        "query_debug_reports",
        "read_debug_evidence",
        "summarize_debug_trace",
    }
    if set(names) != expected:
        raise RuntimeError(f"unexpected MCP tools: {names}")
    if summary.is_error or primitives.is_error or reports.is_error or not rejected.is_error:
        raise RuntimeError("MCP tool success/error contract failed")
    return {
        "status": "passed",
        "transport": "stdio",
        "tools": names,
        "trace_path": trace_path,
        "trace_summary": summary.structured_content,
        "primitive_query": primitives.structured_content,
        "report_query": reports.structured_content,
        "path_traversal_rejected": rejected.is_error,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.root, args.trace))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
