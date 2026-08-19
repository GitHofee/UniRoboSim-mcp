"""MCP v2 binding for bounded evidence inspection and owned simulation control."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Image

    _MCP_SDK_V2 = True
except ImportError:  # MCP 1.10 compatibility for SDK-pinned simulator environments
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[import-not-found,no-redef]
    from mcp.server.fastmcp import Image  # type: ignore[no-redef]

    _MCP_SDK_V2 = False

from . import __version__
from .control import SimulationControl
from .query import query_events, query_primitives, query_reports, summarize_trace
from .store import EvidenceLimits, EvidenceStore


def create_server(
    evidence_root: str | Path,
    *,
    limits: EvidenceLimits | None = None,
    control: SimulationControl | None = None,
) -> Any:
    """Create a server with evidence tools and optional explicit simulation control."""

    store = EvidenceStore(evidence_root, limits=limits)
    instructions = (
        "Use evidence summaries and scene snapshots before requesting detailed values. Evidence paths are relative "
        "to the configured root. Simulation writes are available only when explicitly enabled; they require the "
        "session lease and a unique command_id. Reads and backend-camera screenshots do not require the lease."
    )
    if _MCP_SDK_V2:
        server = MCPServer(
            name="unirobosim",
            title="UniRoboSim MCP",
            version=__version__,
            description="Bounded UniRoboSim evidence inspection with optional owned-session simulation control.",
            instructions=instructions,
        )
    else:
        server = MCPServer(name="unirobosim", instructions=instructions)

    @server.tool()
    def evidence_server_info() -> dict[str, Any]:
        """Report the active read-only boundary and hard response/query limits."""

        configured = store.limits
        return {
            "evidence_root": str(store.root),
            "read_only": True,
            "simulation_control_enabled": control is not None,
            "limits": {
                "max_file_bytes": configured.max_file_bytes,
                "max_text_return_bytes": configured.max_text_return_bytes,
                "max_trace_events": configured.max_trace_events,
                "max_results": configured.max_results,
                "max_query_items": configured.max_query_items,
                "max_scanned_files": configured.max_scanned_files,
            },
        }

    @server.tool()
    def list_debug_evidence(pattern: str = "*") -> dict[str, Any]:
        """List allowlisted evidence files below the root using a bounded POSIX glob."""

        return store.list_files(pattern=pattern)

    @server.tool()
    def read_debug_evidence(relative_path: str, parse_json: bool = False) -> dict[str, Any]:
        """Read one bounded UTF-8 evidence file, optionally decoding it as JSON."""

        if not isinstance(parse_json, bool):
            raise ValueError("parse_json must be boolean")
        return store.read_json(relative_path) if parse_json else store.read_text(relative_path)

    @server.tool()
    def summarize_debug_trace(trace_path: str) -> dict[str, Any]:
        """Validate an entire closed trace and return its compact manifest."""

        return {"path": trace_path, **summarize_trace(store.read_trace(trace_path))}

    @server.tool()
    def query_debug_events(
        trace_path: str,
        event_kind: str | None = None,
        start_sequence: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Query publish/clear/reset events without returning full geometry payloads."""

        return {
            "path": trace_path,
            **query_events(
                store.read_trace(trace_path),
                event_kind=event_kind,
                start_sequence=start_sequence,
                limit=limit,
                max_items=store.limits.max_query_items,
            ),
        }

    @server.tool()
    def query_debug_reports(
        trace_path: str,
        start_report_sequence: int = 1,
        only_dropped: bool = False,
        drop_reason: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Query accepted, filtered and dropped publish decisions without geometry."""

        return {
            "path": trace_path,
            **query_reports(
                store.read_trace(trace_path),
                start_report_sequence=start_report_sequence,
                only_dropped=only_dropped,
                drop_reason=drop_reason,
                limit=limit,
                max_items=store.limits.max_query_items,
            ),
        }

    @server.tool()
    def query_debug_primitives(
        trace_path: str,
        sequence: int | None = None,
        layer: str | None = None,
        group: str | None = None,
        primitive_id: str | None = None,
        primitive_kind: str | None = None,
        environment_index: int | None = None,
        include_geometry: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Inspect the reconstructed active primitive state at a trace sequence."""

        return {
            "path": trace_path,
            **query_primitives(
                store.read_trace(trace_path),
                sequence=sequence,
                layer=layer,
                group=group,
                primitive_id=primitive_id,
                primitive_kind=primitive_kind,
                environment_index=environment_index,
                include_geometry=include_geometry,
                limit=limit,
                max_items=store.limits.max_query_items,
            ),
        }

    if control is not None:

        @server.tool()
        def simulation_control_info() -> dict[str, Any]:
            """Report simulation ownership, lease policy, allowlisted roots, and resource limits."""

            return control.info()

        @server.tool()
        def simulation_list_backends() -> dict[str, Any]:
            """Probe the test backend and installed UniRoboSim backend entry points."""

            return control.list_backends()

        @server.tool()
        def simulation_list_sessions() -> dict[str, Any]:
            """List only the bounded simulation sessions owned by this MCP server."""

            return control.list_sessions()

        @server.tool()
        def simulation_create(
            backend: str,
            world_id: str = "mcp",
            num_envs: int = 1,
            time_step_seconds: float = 1.0 / 60.0,
            gravity_m_s2: list[float] | None = None,
        ) -> dict[str, Any]:
            """Create an owned configuring session and return its opaque write lease."""

            return control.create(
                backend=backend,
                world_id=world_id,
                num_envs=num_envs,
                time_step_seconds=time_step_seconds,
                gravity_m_s2=(0.0, 0.0, -9.81) if gravity_m_s2 is None else gravity_m_s2,
            )

        @server.tool()
        def simulation_configure_entity(
            session_id: str,
            lease_id: str,
            command_id: str,
            entity: dict[str, Any],
        ) -> dict[str, Any]:
            """Add one bounded EasyAPI entity while an owned session is configuring."""

            return control.configure_entity(session_id, lease_id, command_id, entity)

        @server.tool()
        def simulation_start(session_id: str, lease_id: str, command_id: str) -> dict[str, Any]:
            """Compile and start the configured scene using the selected backend."""

            return control.start(session_id, lease_id, command_id)

        @server.tool()
        def simulation_renew_lease(session_id: str, lease_id: str, command_id: str) -> dict[str, Any]:
            """Extend the write lease for an owned live session."""

            return control.renew_lease(session_id, lease_id, command_id)

        @server.tool()
        def simulation_step(
            session_id: str,
            lease_id: str,
            command_id: str,
            count: int = 1,
        ) -> dict[str, Any]:
            """Advance a running simulation by a bounded number of steps."""

            return control.step(session_id, lease_id, command_id, count)

        @server.tool()
        def simulation_reset(
            session_id: str,
            lease_id: str,
            command_id: str,
            environments: list[int] | None = None,
        ) -> dict[str, Any]:
            """Reset all or selected environments in a running session."""

            return control.reset(session_id, lease_id, command_id, environments)

        @server.tool()
        def simulation_command(
            session_id: str,
            lease_id: str,
            command_id: str,
            command: dict[str, Any],
        ) -> dict[str, Any]:
            """Apply an articulation, rigid, soft-matter, scene, or debug command."""

            return control.command(session_id, lease_id, command_id, command)

        @server.tool()
        def simulation_get_entity(
            session_id: str,
            entity_name: str,
            include_values: bool = False,
            include_contact: bool = False,
        ) -> dict[str, Any]:
            """Read typed state and metadata for one rigid, articulated, soft, fluid, or camera entity."""

            return control.get_entity(
                session_id,
                entity_name,
                include_values=include_values,
                include_contact=include_contact,
            )

        @server.tool()
        def simulation_scene_snapshot(session_id: str) -> dict[str, Any]:
            """Read the backend-neutral scene graph for discovery and spatial reasoning."""

            return control.scene_snapshot(session_id)

        @server.tool(structured_output=False)
        def simulation_capture_camera(
            session_id: str,
            camera_name: str,
            environment_index: int = 0,
            save_to_evidence: bool = False,
            filename: str | None = None,
        ) -> Any:
            """Return a PNG image encoded from the real backend RGB camera buffer."""

            screenshot = control.capture_camera(
                session_id,
                camera_name,
                environment_index=environment_index,
                save_to_evidence=save_to_evidence,
                filename=filename,
            )
            return [screenshot.metadata, Image(data=screenshot.png, format="png")]

        @server.tool()
        def simulation_close(session_id: str, lease_id: str, command_id: str) -> dict[str, Any]:
            """Close an owned simulation session and release its backend resources."""

            return control.close(session_id, lease_id, command_id)

    return server
