from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import ImageContent

from unirobosim_mcp import __version__
from unirobosim_mcp.cli import main
from unirobosim_mcp.control import SimulationControl
from unirobosim_mcp.server import create_server


def test_mcp_in_process_discovery_and_calls(evidence_root: Path) -> None:
    async def scenario() -> None:
        server = create_server(evidence_root)
        async with Client(server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "evidence_server_info",
                "list_debug_evidence",
                "query_debug_events",
                "query_debug_primitives",
                "query_debug_reports",
                "read_debug_evidence",
                "summarize_debug_trace",
            }
            info = await client.call_tool("evidence_server_info", {})
            assert info.structured_content["read_only"] is True
            listing = await client.call_tool("list_debug_evidence", {"pattern": "*.json"})
            assert listing.structured_content["count"] == 1
            text = await client.call_tool("read_debug_evidence", {"relative_path": "notes.md"})
            assert text.structured_content["content"] == "evidence notes"
            decoded = await client.call_tool(
                "read_debug_evidence",
                {"relative_path": "result.json", "parse_json": True},
            )
            assert decoded.structured_content["value"] == {"status": "passed"}
            summary = await client.call_tool(
                "summarize_debug_trace",
                {"trace_path": "run/sample.urs-debug.jsonl"},
            )
            assert summary.structured_content["active_count"] == 1
            events = await client.call_tool(
                "query_debug_events",
                {"trace_path": "run/sample.urs-debug.jsonl", "event_kind": "clear"},
            )
            assert events.structured_content["count"] == 1
            reports = await client.call_tool(
                "query_debug_reports",
                {
                    "trace_path": "run/sample.urs-debug.jsonl",
                    "only_dropped": True,
                    "drop_reason": "event_rate",
                },
            )
            assert reports.structured_content["count"] == 1
            query = await client.call_tool(
                "query_debug_primitives",
                {
                    "trace_path": "run/sample.urs-debug.jsonl",
                    "primitive_kind": "line_list",
                    "include_geometry": True,
                },
            )
            assert query.structured_content["count"] == 1
            rejected = await client.call_tool("read_debug_evidence", {"relative_path": "../secret"})
            assert rejected.is_error is True

    asyncio.run(scenario())


def test_every_control_tool_through_real_mcp_client(evidence_root: Path) -> None:
    async def scenario() -> None:
        control = SimulationControl(evidence_root)
        server = create_server(evidence_root, control=control)
        async with Client(server) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names >= {
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
            info = await client.call_tool("simulation_control_info", {})
            assert info.structured_content["enabled"] is True
            backends = await client.call_tool("simulation_list_backends", {})
            assert any(item["backend"] == "fake" for item in backends.structured_content["backends"])
            before = await client.call_tool("simulation_list_sessions", {})
            assert before.structured_content["sessions"] == []
            created = await client.call_tool("simulation_create", {"backend": "fake", "num_envs": 1})
            session_id = created.structured_content["session_id"]
            lease_id = created.structured_content["lease_id"]
            for command_id, entity in (
                ("add-box", {"kind": "box", "name": "box", "color_rgba": [0.2, 0.55, 0.9, 1.0]}),
                (
                    "add-camera",
                    {"kind": "camera", "name": "camera", "resolution": [32, 24], "outputs": ["rgb"]},
                ),
            ):
                configured = await client.call_tool(
                    "simulation_configure_entity",
                    {"session_id": session_id, "lease_id": lease_id, "command_id": command_id, "entity": entity},
                )
                assert configured.is_error is False
            started = await client.call_tool(
                "simulation_start",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "start"},
            )
            assert started.structured_content["phase"] == "running"
            renewed = await client.call_tool(
                "simulation_renew_lease",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "renew"},
            )
            assert renewed.is_error is False
            commanded = await client.call_tool(
                "simulation_command",
                {
                    "session_id": session_id,
                    "lease_id": lease_id,
                    "command_id": "wrench",
                    "command": {"kind": "rigid_wrench", "entity": "box", "force_n": [1.0, 0.0, 0.0]},
                },
            )
            assert commanded.structured_content["accepted"] is True
            stepped = await client.call_tool(
                "simulation_step",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "step", "count": 2},
            )
            assert stepped.structured_content["tick"]["step_index"] == 2
            entity_state = await client.call_tool(
                "simulation_get_entity",
                {"session_id": session_id, "entity_name": "box", "include_values": True},
            )
            assert entity_state.structured_content["path"] == "/box"
            snapshot = await client.call_tool("simulation_scene_snapshot", {"session_id": session_id})
            assert len(snapshot.structured_content["snapshot"]["entities"]) == 2
            screenshot = await client.call_tool(
                "simulation_capture_camera",
                {
                    "session_id": session_id,
                    "camera_name": "camera",
                    "save_to_evidence": True,
                    "filename": "mcp-camera.png",
                },
            )
            image_blocks = [item for item in screenshot.content if isinstance(item, ImageContent)]
            assert len(image_blocks) == 1 and image_blocks[0].mime_type == "image/png"
            assert (evidence_root / "screenshots" / "mcp-camera.png").exists()
            reset = await client.call_tool(
                "simulation_reset",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "reset"},
            )
            assert reset.is_error is False
            closed = await client.call_tool(
                "simulation_close",
                {"session_id": session_id, "lease_id": lease_id, "command_id": "close"},
            )
            assert closed.structured_content["phase"] == "closed"

    asyncio.run(scenario())


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, transport: str, **kwargs: Any) -> None:
        self.calls.append((transport, kwargs))


def test_cli_stdio_and_http_routes(monkeypatch: pytest.MonkeyPatch, evidence_root: Path) -> None:
    fake = FakeServer()
    roots: list[Path] = []

    def factory(root: Path, **_: Any) -> FakeServer:
        roots.append(root)
        return fake

    monkeypatch.setattr("unirobosim_mcp.cli.create_server", factory)
    main(["--root", str(evidence_root)])
    main(
        [
            "--root",
            str(evidence_root),
            "--transport",
            "streamable-http",
            "--host",
            "localhost",
            "--port",
            "9000",
        ]
    )
    assert roots == [evidence_root, evidence_root]
    assert fake.calls == [("stdio", {}), ("streamable-http", {"host": "localhost", "port": 9000})]


def test_cli_environment_root_and_safety_errors(
    monkeypatch: pytest.MonkeyPatch,
    evidence_root: Path,
) -> None:
    fake = FakeServer()
    monkeypatch.setattr("unirobosim_mcp.cli.create_server", lambda root, **kwargs: fake)
    monkeypatch.setenv("UNIROBOSIM_EVIDENCE_ROOT", str(evidence_root))
    main([])
    assert fake.calls == [("stdio", {})]
    for args in (
        ["--root", str(evidence_root), "--transport", "streamable-http", "--host", "0.0.0.0"],
        ["--root", str(evidence_root), "--port", "0"],
    ):
        with pytest.raises(SystemExit):
            main(args)
    monkeypatch.delenv("UNIROBOSIM_EVIDENCE_ROOT")
    with pytest.raises(SystemExit):
        main([])


def test_cli_enables_owned_control(monkeypatch: pytest.MonkeyPatch, evidence_root: Path) -> None:
    fake = FakeServer()
    captured: list[Any] = []

    def factory(root: Path, **kwargs: Any) -> FakeServer:
        captured.append(kwargs["control"])
        return fake

    monkeypatch.setattr("unirobosim_mcp.cli.create_server", factory)
    main(
        [
            "--root",
            str(evidence_root),
            "--enable-control",
            "--asset-root",
            str(evidence_root),
            "--max-sessions",
            "1",
            "--lease-timeout-seconds",
            "10",
        ]
    )
    assert isinstance(captured[0], SimulationControl)
    assert captured[0].limits.max_sessions == 1
    assert fake.calls == [("stdio", {})]
    with pytest.raises(SystemExit):
        main(["--root", str(evidence_root), "--asset-root", str(evidence_root)])


def test_release_identity_is_consistent() -> None:
    assert __version__ == "0.7.0"
