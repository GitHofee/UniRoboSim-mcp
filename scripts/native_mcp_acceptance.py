#!/usr/bin/env python3
"""Exercise every public UniRoboSim MCP tool against one selected backend."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from unirobosim import (
    ArrayValue,
    DebugBatch,
    DebugBus,
    DebugLifetime,
    DebugPrimitive,
    DebugPrimitiveKind,
    TraceDebugSink,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True, choices=("isaaclab", "mujoco", "pybullet"))
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--box-name", default="test_box")
    parser.add_argument("--box-color", nargs=4, type=float, default=(0.2, 0.55, 0.9, 1.0))
    parser.add_argument("--box-size", nargs=3, type=float, default=(0.12, 0.12, 0.12))
    parser.add_argument("--articulation-asset", type=Path)
    parser.add_argument("--articulation-joints", nargs="+", default=["hinge"])
    parser.add_argument(
        "--installed-server",
        action="store_true",
        help="launch the installed unirobosim-mcp package instead of the source tree",
    )
    return parser


def _make_trace(root: Path) -> Path:
    trace = root / "trace" / "native-acceptance.urs-debug.jsonl"
    sink = TraceDebugSink(trace, run_id="native-mcp-acceptance")
    bus = DebugBus((sink,))
    primitive = DebugPrimitive(
        "origin",
        "acceptance",
        DebugPrimitiveKind.POINT_SET,
        ArrayValue.from_nested([[[0.0, 0.0, 0.0]]]),
        (0,),
        group="origin",
        lifetime=DebugLifetime.persistent(),
    )
    bus.publish(DebugBatch((primitive,), step_index=0, sim_time_s=0.0, event_id="origin"))
    bus.close()
    return trace


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return value


def _structured(result: Any) -> dict[str, Any]:
    value = getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "structuredContent", None)
    return {} if value is None else dict(value)


def _is_error(result: Any) -> bool:
    value = getattr(result, "is_error", None)
    if value is None:
        value = getattr(result, "isError", False)
    return bool(value)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence.resolve()
    root.mkdir(parents=True, exist_ok=True)
    trace = _make_trace(root)
    articulation_asset = None if args.articulation_asset is None else args.articulation_asset.resolve(strict=True)
    joint_names = tuple(str(name) for name in args.articulation_joints)
    if not joint_names:
        raise ValueError("at least one articulation joint is required")
    box_name = str(args.box_name)
    if not box_name or box_name.startswith("/"):
        raise ValueError("box name must be a non-empty relative entity name")
    box_color = tuple(float(value) for value in args.box_color)
    box_size = tuple(float(value) for value in args.box_size)
    calls: list[dict[str, Any]] = []
    server_env = dict(os.environ)
    if not args.installed_server:
        source_root = Path(__file__).resolve().parents[1] / "src"
        existing_pythonpath = server_env.get("PYTHONPATH")
        server_env["PYTHONPATH"] = (
            str(source_root) if not existing_pythonpath else f"{source_root}:{existing_pythonpath}"
        )
    server_args = [
        "-m",
        "unirobosim_mcp.cli",
        "--root",
        str(root),
        "--enable-control",
        "--max-sessions",
        "1",
        "--lease-timeout-seconds",
        "600",
    ]
    if articulation_asset is not None:
        server_args.extend(("--asset-root", str(articulation_asset.parent)))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=server_args,
        env=server_env,
    )
    session_id = ""
    lease_id = ""

    async with stdio_client(parameters) as (read_stream, write_stream):  # noqa: SIM117
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            async def call(name: str, arguments: dict[str, Any]) -> Any:
                started = time.monotonic()
                result = await session.call_tool(name, arguments)
                elapsed = time.monotonic() - started
                record: dict[str, Any] = {
                    "tool": name,
                    "passed": not _is_error(result),
                    "elapsed_seconds": elapsed,
                }
                structured = _structured(result)
                if structured:
                    safe = dict(structured)
                    safe.pop("lease_id", None)
                    record["structured"] = safe
                if name == "simulation_capture_camera":
                    content = _dump(result).get("content", [])
                    record["content_types"] = [item.get("type") for item in content]
                    for item in content:
                        if item.get("type") == "image":
                            record["image_mime_type"] = item.get("mimeType")
                            record["image_base64_bytes"] = len(item.get("data", ""))
                calls.append(record)
                if _is_error(result):
                    raise RuntimeError(f"MCP tool {name} failed: {_dump(result)}")
                return result

            tools = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools.tools)

            await call("evidence_server_info", {})
            await call("list_debug_evidence", {"pattern": "**/*"})
            await call("read_debug_evidence", {"relative_path": str(trace.relative_to(root))})
            await call("summarize_debug_trace", {"trace_path": str(trace.relative_to(root))})
            await call(
                "query_debug_events",
                {"trace_path": str(trace.relative_to(root)), "event_kind": "publish"},
            )
            await call("query_debug_reports", {"trace_path": str(trace.relative_to(root))})
            await call(
                "query_debug_primitives",
                {"trace_path": str(trace.relative_to(root)), "include_geometry": True},
            )

            await call("simulation_control_info", {})
            await call("simulation_list_backends", {})
            await call("simulation_list_sessions", {})
            created = await call(
                "simulation_create",
                {
                    "backend": args.backend,
                    "world_id": f"mcp-{args.backend}-acceptance",
                    "num_envs": 1,
                    "time_step_seconds": 1.0 / 120.0,
                },
            )
            created_value = _structured(created)
            session_id = str(created_value["session_id"])
            lease_id = str(created_value["lease_id"])

            articulation_entity: dict[str, Any] = {
                "kind": "articulation",
                "name": "door",
                "joint_names": list(joint_names),
                "initial_positions": [0.0] * len(joint_names),
            }
            if articulation_asset is not None:
                articulation_entity["asset_uri"] = str(articulation_asset)

            for command_id, entity in (
                (
                    "configure-box",
                    {
                        "kind": "box",
                        "name": box_name,
                        "size_m": list(box_size),
                        "mass_kg": 0.2,
                        "color_rgba": list(box_color),
                        "position_m": [0.0, 0.0, 0.6],
                    },
                ),
                (
                    "configure-door",
                    articulation_entity,
                ),
                (
                    "configure-camera",
                    {
                        "kind": "camera",
                        "name": "camera",
                        "resolution": [args.width, args.height],
                        "outputs": ["rgb", "depth"],
                        "position_m": [2.0, 0.0, 1.5],
                    },
                ),
            ):
                await call(
                    "simulation_configure_entity",
                    {
                        "session_id": session_id,
                        "lease_id": lease_id,
                        "command_id": command_id,
                        "entity": entity,
                    },
                )

            await call(
                "simulation_start",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "start"},
            )
            await call(
                "simulation_renew_lease",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "renew"},
            )
            await call(
                "simulation_command",
                {
                    "session_id": session_id,
                    "lease_id": lease_id,
                    "command_id": "door-position",
                    "command": {
                        "kind": "articulation",
                        "entity": "door",
                        "targets": [0.25, *([0.0] * (len(joint_names) - 1))],
                    },
                },
            )
            await call(
                "simulation_command",
                {
                    "session_id": session_id,
                    "lease_id": lease_id,
                    "command_id": "box-wrench",
                    "command": {"kind": "rigid_wrench", "entity": box_name, "force_n": [0.2, 0.0, 0.0]},
                },
            )
            await call(
                "simulation_step",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "step", "count": 4},
            )
            await call(
                "simulation_get_entity",
                {"session_id": session_id, "entity_name": box_name, "include_values": True},
            )
            await call(
                "simulation_get_entity",
                {"session_id": session_id, "entity_name": "door", "include_values": True},
            )
            await call(
                "simulation_get_entity",
                {"session_id": session_id, "entity_name": "camera"},
            )
            await call("simulation_scene_snapshot", {"session_id": session_id})
            await call(
                "simulation_capture_camera",
                {
                    "session_id": session_id,
                    "camera_name": "camera",
                    "environment_index": 0,
                    "save_to_evidence": True,
                    "filename": f"{args.backend}-mcp-camera.png",
                },
            )
            await call(
                "simulation_command",
                {
                    "session_id": session_id,
                    "lease_id": lease_id,
                    "command_id": "set-box-pose",
                    "command": {
                        "kind": "scene",
                        "scene_kind": "set_pose",
                        "entity": box_name,
                        "target_pose": {
                            "position_m": [0.15, 0.0, 0.6],
                            "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
                        },
                    },
                },
            )
            await call(
                "simulation_reset",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "reset"},
            )
            await call("simulation_list_sessions", {})
            await call(
                "simulation_close",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "close"},
            )

    required = {
        "evidence_server_info",
        "list_debug_evidence",
        "read_debug_evidence",
        "summarize_debug_trace",
        "query_debug_events",
        "query_debug_reports",
        "query_debug_primitives",
        "simulation_control_info",
        "simulation_list_backends",
        "simulation_list_sessions",
        "simulation_create",
        "simulation_configure_entity",
        "simulation_start",
        "simulation_renew_lease",
        "simulation_step",
        "simulation_reset",
        "simulation_command",
        "simulation_get_entity",
        "simulation_scene_snapshot",
        "simulation_capture_camera",
        "simulation_close",
    }
    return {
        "schema": "unirobosim.mcp.native-acceptance/v1",
        "backend": args.backend,
        "python": sys.version,
        "tool_names": tool_names,
        "all_tools_present": required <= set(tool_names),
        "all_calls_passed": all(call["passed"] for call in calls),
        "called_tools": sorted({call["tool"] for call in calls}),
        "calls": calls,
        "camera_evidence": str(root / "screenshots" / f"{args.backend}-mcp-camera.png"),
        "audit_evidence": str(root / "mcp-control-audit.jsonl"),
        "articulation_asset": None if articulation_asset is None else str(articulation_asset),
        "installed_server": bool(args.installed_server),
        "box_fixture": {"name": box_name, "color_rgba": box_color, "size_m": box_size},
    }


def main() -> None:
    args = _parser().parse_args()
    report = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("backend", "all_tools_present", "all_calls_passed")}, indent=2))
    if not report["all_tools_present"] or not report["all_calls_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
