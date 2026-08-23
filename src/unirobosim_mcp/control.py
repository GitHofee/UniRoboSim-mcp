"""Bounded, lease-protected simulation control for MCP clients."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from unirobosim import (
    ArrayValue,
    Articulation,
    Camera,
    Deformable,
    Entity,
    EntityPath,
    ParticleFluid,
    Pose,
    RigidBody,
    SceneCommand,
    SceneCommandKind,
    SceneControlWorld,
    SceneDragMode,
    Sim,
)
from unirobosim.testing import FakeProvider

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


class ControlAccessError(ValueError):
    """A control request crossed a configured ownership or resource boundary."""


@dataclass(frozen=True)
class ControlLimits:
    """Hard limits applied before a request reaches a simulator backend."""

    max_sessions: int = 2
    max_entities_per_session: int = 128
    max_environments: int = 64
    max_steps_per_call: int = 1_000
    max_points_per_entity: int = 100_000
    max_camera_pixels: int = 1920 * 1080
    max_observation_values: int = 100_000
    max_cached_commands: int = 2_048
    lease_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_sessions,
            self.max_entities_per_session,
            self.max_environments,
            self.max_steps_per_call,
            self.max_points_per_entity,
            self.max_camera_pixels,
            self.max_observation_values,
            self.max_cached_commands,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in integer_limits):
            raise ValueError("control integer limits must be positive integers")
        if (
            not isinstance(self.lease_timeout_seconds, (int, float))
            or isinstance(self.lease_timeout_seconds, bool)
            or self.lease_timeout_seconds <= 0.0
        ):
            raise ValueError("lease_timeout_seconds must be positive")


@dataclass(frozen=True)
class Screenshot:
    png: bytes
    metadata: dict[str, Any]


@dataclass
class _CommandRecord:
    digest: str
    result: dict[str, Any]


@dataclass
class _OwnedSession:
    session_id: str
    lease_id: str
    backend: str
    sim: Sim
    phase: str
    expires_at: float
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: OrderedDict[str, _CommandRecord] = field(default_factory=OrderedDict)


def _tick_dict(tick: Any) -> dict[str, float | int]:
    return {"step_index": tick.step_index, "sim_time_seconds": tick.sim_time_seconds}


def _array_dict(value: ArrayValue, *, include_values: bool, max_values: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "shape": list(value.shape),
        "dtype": value.dtype,
        "device": value.device,
        "value_count": len(value.values),
    }
    if include_values:
        selected = value.values[:max_values]
        result["values"] = list(selected)
        result["values_complete"] = len(selected) == len(value.values)
    return result


def _build_report_dict(report: Any) -> dict[str, Any]:
    return {
        "world_id": report.world_id,
        "generation": report.generation,
        "environment_count": report.environment_count,
        "entity_count": report.entity_count,
        "fingerprint": report.fingerprint.to_dict(),
    }


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def encode_rgb_png(rgb: ArrayValue, environment_index: int) -> tuple[bytes, int, int]:
    """Encode one environment of a canonical uint8 [N,H,W,3] RGB array."""

    if rgb.dtype != "uint8" or len(rgb.shape) != 4 or rgb.shape[-1] != 3:
        raise ValueError("camera RGB data must have uint8 shape [environment,height,width,3]")
    environments, height, width, _ = rgb.shape
    if not isinstance(environment_index, int) or isinstance(environment_index, bool):
        raise ValueError("environment_index must be an integer")
    if not 0 <= environment_index < environments:
        raise ValueError("environment_index is outside the camera batch")
    row_bytes = width * 3
    start = environment_index * height * row_bytes
    raw = rgb.to_bytes()[start : start + height * row_bytes]
    scanlines = b"".join(b"\x00" + raw[offset : offset + row_bytes] for offset in range(0, len(raw), row_bytes))
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        signature
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b""),
        width,
        height,
    )


class SimulationControl:
    """Own and control bounded UniRoboSim EasyAPI sessions.

    The manager never attaches to a session created by another process. Mutations
    require both the opaque lease returned by :meth:`create` and an idempotency key.
    State reads and camera captures require only the session identifier.
    """

    def __init__(
        self,
        evidence_root: str | Path,
        *,
        asset_roots: tuple[str | Path, ...] = (),
        limits: ControlLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.evidence_root = Path(evidence_root).resolve()
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.asset_roots = tuple(Path(root).resolve() for root in asset_roots)
        self.limits = limits or ControlLimits()
        self._clock = clock
        self._sessions: dict[str, _OwnedSession] = {}
        self._lock = threading.RLock()
        self._audit_path = self.evidence_root / "mcp-control-audit.jsonl"

    def info(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "ownership": "server-created-sessions-only",
            "read_requires_lease": False,
            "mutation_requires_lease": True,
            "mutation_requires_command_id": True,
            "asset_roots": [str(path) for path in self.asset_roots],
            "audit_path": str(self._audit_path),
            "limits": {
                "max_sessions": self.limits.max_sessions,
                "max_entities_per_session": self.limits.max_entities_per_session,
                "max_environments": self.limits.max_environments,
                "max_steps_per_call": self.limits.max_steps_per_call,
                "max_points_per_entity": self.limits.max_points_per_entity,
                "max_camera_pixels": self.limits.max_camera_pixels,
                "max_observation_values": self.limits.max_observation_values,
                "max_cached_commands": self.limits.max_cached_commands,
                "lease_timeout_seconds": self.limits.lease_timeout_seconds,
            },
        }

    def list_backends(self) -> dict[str, Any]:
        reports: list[dict[str, Any]] = [self._probe_provider("fake", FakeProvider)]
        seen = {"fake"}
        for entry_point in sorted(metadata.entry_points(group="unirobosim.backends"), key=lambda item: item.name):
            if entry_point.name in seen:
                continue
            seen.add(entry_point.name)
            reports.append(self._probe_entry_point(entry_point))
        return {"backends": reports}

    @staticmethod
    def _probe_provider(name: str, factory: Callable[[], Any]) -> dict[str, Any]:
        try:
            provider = factory()
            report = provider.probe()
            descriptor = provider.descriptor
            return {
                "backend": name,
                "provider_id": descriptor.provider_id,
                "version": descriptor.version,
                "contract_version": descriptor.contract_version,
                "available": report.available,
                "reason": report.reason,
                "capabilities": [item.to_dict() for item in descriptor.capabilities],
            }
        except Exception as exc:
            return {"backend": name, "available": False, "error": f"{type(exc).__name__}: {exc}"}

    @classmethod
    def _probe_entry_point(cls, entry_point: metadata.EntryPoint) -> dict[str, Any]:
        try:
            return cls._probe_provider(entry_point.name, entry_point.load())
        except Exception as exc:
            return {"backend": entry_point.name, "available": False, "error": f"{type(exc).__name__}: {exc}"}

    def list_sessions(self) -> dict[str, Any]:
        with self._lock:
            self._reap_expired()
            now = self._clock()
            sessions = [
                {
                    "session_id": item.session_id,
                    "backend": item.backend,
                    "phase": item.phase,
                    "entity_count": len(item.entities),
                    "lease_remaining_seconds": max(0.0, item.expires_at - now),
                }
                for item in self._sessions.values()
            ]
        return {"sessions": sorted(sessions, key=lambda item: item["session_id"])}

    def create(
        self,
        *,
        backend: str,
        world_id: str = "mcp",
        num_envs: int = 1,
        time_step_seconds: float = 1.0 / 60.0,
        gravity_m_s2: list[float] | tuple[float, ...] = (0.0, 0.0, -9.81),
    ) -> dict[str, Any]:
        if not isinstance(backend, str) or not backend:
            raise ValueError("backend must be a non-empty string")
        if (
            not isinstance(num_envs, int)
            or isinstance(num_envs, bool)
            or not 1 <= num_envs <= self.limits.max_environments
        ):
            raise ValueError(f"num_envs must be between 1 and {self.limits.max_environments}")
        with self._lock:
            self._reap_expired()
            active = sum(item.phase != "closed" for item in self._sessions.values())
            if active >= self.limits.max_sessions:
                raise ControlAccessError("maximum owned simulation sessions reached")
            provider = FakeProvider() if backend in {"fake", "reference.fake"} else None
            selected_backend = "fake" if provider is not None else backend
            sim = Sim(
                backend=selected_backend,
                provider=provider,
                world_id=world_id,
                num_envs=num_envs,
                time_step_seconds=time_step_seconds,
                gravity_m_s2=gravity_m_s2,
            )
            session_id = uuid4().hex
            lease_id = uuid4().hex
            owned = _OwnedSession(
                session_id,
                lease_id,
                selected_backend,
                sim,
                "configuring",
                self._clock() + self.limits.lease_timeout_seconds,
            )
            self._sessions[session_id] = owned
            result = {
                "session_id": session_id,
                "lease_id": lease_id,
                "backend": selected_backend,
                "phase": owned.phase,
                "lease_timeout_seconds": self.limits.lease_timeout_seconds,
            }
            self._audit("simulation_create", session_id, None, "applied", {"backend": selected_backend})
            return result

    def configure_entity(
        self,
        session_id: str,
        lease_id: str,
        command_id: str,
        entity: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(entity, dict):
            raise ValueError("entity must be an object")
        return self._execute(
            session_id,
            lease_id,
            command_id,
            "simulation_configure_entity",
            entity,
            lambda owned: self._configure(owned, entity),
        )

    def _configure(self, owned: _OwnedSession, entity: dict[str, Any]) -> dict[str, Any]:
        if owned.phase != "configuring":
            raise ControlAccessError("entities can only be added before simulation_start")
        if len(owned.entities) >= self.limits.max_entities_per_session:
            raise ControlAccessError("maximum entities per session reached")
        kind = self._required_text(entity, "kind")
        name = self._required_text(entity, "name")
        common = {
            "position_m": entity.get("position_m", (0.0, 0.0, 0.0)),
            "orientation_xyzw": entity.get("orientation_xyzw", (0.0, 0.0, 0.0, 1.0)),
        }
        created: Entity
        if kind == "box":
            created = owned.sim.add_box(
                name,
                size_m=entity.get("size_m", 0.5),
                mass_kg=entity.get("mass_kg", 1.0),
                color_rgba=entity.get("color_rgba", (0.15, 0.7, 0.95, 1.0)),
                static_friction=entity.get("static_friction", 1.0),
                dynamic_friction=entity.get("dynamic_friction", 1.0),
                restitution=entity.get("restitution", 0.0),
                **common,
            )
        elif kind == "rigid_body":
            created = owned.sim.add_rigid_body(name, asset_uri=self._asset_uri(entity), **common)
        elif kind == "articulation":
            asset_uri = self._optional_asset_uri(entity)
            created = owned.sim.add_articulation(
                name,
                joint_names=entity.get("joint_names", ()),
                initial_positions=entity.get("initial_positions", ()),
                joint_effort_limits=entity.get("joint_effort_limits", ()),
                asset_uri=asset_uri,
                **common,
            )
        elif kind == "camera":
            resolution = tuple(entity.get("resolution", (640, 480)))
            if len(resolution) != 2 or any(
                not isinstance(value, int) or isinstance(value, bool) for value in resolution
            ):
                raise ValueError("camera resolution must contain integer width and height")
            if resolution[0] * resolution[1] > self.limits.max_camera_pixels:
                raise ControlAccessError("camera resolution exceeds max_camera_pixels")
            created = owned.sim.add_camera(
                name, resolution=resolution, outputs=entity.get("outputs", ("rgb",)), **common
            )
        elif kind == "deformable":
            rest_positions = entity.get("rest_positions_m")
            self._check_points(rest_positions, "rest_positions_m")
            created = owned.sim.add_deformable(
                name,
                rest_positions_m=rest_positions,
                topology=entity.get("topology", "surface"),
                surface_triangles=entity.get("surface_triangles"),
                tetrahedra=entity.get("tetrahedra"),
                initial_velocities_m_s=entity.get("initial_velocities_m_s"),
                kinematic_nodes=entity.get("kinematic_nodes", ()),
                node_mass_kg=entity.get("node_mass_kg", 1.0),
                linear_damping_per_s=entity.get("linear_damping_per_s", 0.0),
                self_collision=entity.get("self_collision", False),
                **common,
            )
        elif kind == "particle_fluid":
            positions = entity.get("positions_m")
            self._check_points(positions, "positions_m")
            created = owned.sim.add_particle_fluid(
                name,
                positions_m=positions,
                initial_velocities_m_s=entity.get("initial_velocities_m_s"),
                particle_radius_m=entity.get("particle_radius_m", 0.01),
                rest_density_kg_m3=entity.get("rest_density_kg_m3", 1000.0),
                particle_mass_kg=entity.get("particle_mass_kg"),
                dynamic_viscosity_pa_s=entity.get("dynamic_viscosity_pa_s", 0.001),
                surface_tension_n_m=entity.get("surface_tension_n_m", 0.072),
                **common,
            )
        else:
            raise ValueError("entity kind must be box, rigid_body, articulation, camera, deformable, or particle_fluid")
        path = created.path.value
        owned.entities[path] = {"kind": created.kind.value, "configuration": dict(entity)}
        return {"session_id": owned.session_id, "path": path, "kind": created.kind.value}

    def start(self, session_id: str, lease_id: str, command_id: str) -> dict[str, Any]:
        def operation(owned: _OwnedSession) -> dict[str, Any]:
            if owned.phase != "configuring":
                raise ControlAccessError("session is not configuring")
            report = owned.sim.start()
            owned.phase = "running"
            return {"session_id": owned.session_id, "phase": owned.phase, "build_report": _build_report_dict(report)}

        return self._execute(session_id, lease_id, command_id, "simulation_start", {}, operation)

    def renew_lease(self, session_id: str, lease_id: str, command_id: str) -> dict[str, Any]:
        return self._execute(
            session_id,
            lease_id,
            command_id,
            "simulation_renew_lease",
            {},
            lambda owned: {
                "session_id": owned.session_id,
                "lease_timeout_seconds": self.limits.lease_timeout_seconds,
            },
        )

    def step(self, session_id: str, lease_id: str, command_id: str, count: int = 1) -> dict[str, Any]:
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= self.limits.max_steps_per_call:
            raise ValueError(f"count must be between 1 and {self.limits.max_steps_per_call}")

        def operation(owned: _OwnedSession) -> dict[str, Any]:
            self._running(owned)
            return {"session_id": owned.session_id, "tick": _tick_dict(owned.sim.step(count))}

        return self._execute(session_id, lease_id, command_id, "simulation_step", {"count": count}, operation)

    def reset(
        self,
        session_id: str,
        lease_id: str,
        command_id: str,
        environments: list[int] | None = None,
    ) -> dict[str, Any]:
        def operation(owned: _OwnedSession) -> dict[str, Any]:
            self._running(owned)
            report = owned.sim.reset(environments)
            return {
                "session_id": owned.session_id,
                "environment_indices": list(report.environment_indices),
                "reset_count": report.reset_count,
                "tick": _tick_dict(report.tick),
            }

        return self._execute(
            session_id,
            lease_id,
            command_id,
            "simulation_reset",
            {"environments": environments},
            operation,
        )

    def command(
        self,
        session_id: str,
        lease_id: str,
        command_id: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(command, dict):
            raise ValueError("command must be an object")
        return self._execute(
            session_id,
            lease_id,
            command_id,
            "simulation_command",
            command,
            lambda owned: self._command(owned, command_id, command),
        )

    def _command(self, owned: _OwnedSession, command_id: str, command: dict[str, Any]) -> dict[str, Any]:
        self._running(owned)
        kind = self._required_text(command, "kind")
        if kind == "articulation":
            entity = owned.sim.entity(self._required_text(command, "entity"))
            if not isinstance(entity, Articulation):
                raise ValueError("target is not an articulation")
            entity.command(
                command.get("targets"),
                mode=command.get("mode", "position"),
                joints=command.get("joints"),
                environments=command.get("environments"),
            )
        elif kind == "rigid_wrench":
            entity = owned.sim.entity(self._required_text(command, "entity"))
            if not isinstance(entity, RigidBody):
                raise ValueError("target is not a rigid body")
            entity.apply_wrench(
                command.get("force_n"),
                command.get("torque_n_m", (0.0, 0.0, 0.0)),
                environments=command.get("environments"),
            )
        elif kind in {"deformable", "particle_fluid"}:
            entity = owned.sim.entity(self._required_text(command, "entity"))
            expected = Deformable if kind == "deformable" else ParticleFluid
            if not isinstance(entity, expected):
                raise ValueError(f"target is not a {kind}")
            selections = command.get("nodes") if kind == "deformable" else command.get("particles")
            selection_name = "nodes" if kind == "deformable" else "particles"
            kwargs = {
                "mode": command.get("mode", "position"),
                selection_name: selections,
                "environments": command.get("environments"),
            }
            entity.command(command.get("targets"), **kwargs)
        elif kind == "scene":
            return self._scene_command(owned, command_id, command)
        elif kind == "debug_clear":
            cleared = owned.sim.debug.clear(
                layer=command.get("layer"),
                group=command.get("group"),
                primitive_id=command.get("primitive_id"),
            )
            return {"session_id": owned.session_id, "kind": kind, "cleared": cleared}
        else:
            raise ValueError("unsupported command kind")
        return {"session_id": owned.session_id, "kind": kind, "accepted": True}

    def _scene_command(self, owned: _OwnedSession, command_id: str, command: dict[str, Any]) -> dict[str, Any]:
        world = owned.sim.world
        if not isinstance(world, SceneControlWorld):
            raise ControlAccessError("backend does not expose scene control")
        kind = SceneCommandKind(self._required_text(command, "scene_kind"))
        pose_value = command.get("target_pose")
        pose = None
        if pose_value is not None:
            if not isinstance(pose_value, dict):
                raise ValueError("target_pose must be an object")
            pose = Pose(tuple(pose_value.get("position_m", ())), tuple(pose_value.get("orientation_xyzw", ())))
        drag_mode_value = command.get("drag_mode")
        scene_command = SceneCommand(
            command_id,
            "unirobosim-mcp",
            owned.lease_id,
            world.generation,
            kind,
            EntityPath(self._entity_path(self._required_text(command, "entity"))),
            environment_index=command.get("environment_index", 0),
            target_pose=pose,
            drag_id=command.get("drag_id"),
            drag_mode=None if drag_mode_value is None else SceneDragMode(drag_mode_value),
            grab_point_world_m=command.get("grab_point_world_m"),
        )
        return {
            "session_id": owned.session_id,
            "kind": "scene",
            "result": world.apply_scene_command(scene_command).to_dict(),
        }

    def get_entity(
        self,
        session_id: str,
        entity_name: str,
        *,
        include_values: bool = False,
        include_contact: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(include_values, bool) or not isinstance(include_contact, bool):
            raise ValueError("include_values and include_contact must be boolean")
        with self._lock:
            owned = self._owned(session_id)
            self._running(owned)
            entity = owned.sim.entity(entity_name)
            arrays: dict[str, Any] = {}
            tick: Any
            if isinstance(entity, RigidBody):
                rigid_state = entity.state
                arrays = {
                    "positions_m": rigid_state.positions_m,
                    "orientations_xyzw": rigid_state.orientations_xyzw,
                    "linear_velocities_m_s": rigid_state.linear_velocities_m_s,
                    "angular_velocities_rad_s": rigid_state.angular_velocities_rad_s,
                }
                tick = rigid_state.tick
            elif isinstance(entity, Articulation):
                articulation_state = entity.state
                arrays = {
                    "joint_positions": articulation_state.joint_positions,
                    "joint_velocities": articulation_state.joint_velocities,
                }
                tick = articulation_state.tick
            elif isinstance(entity, Deformable):
                deformable_state = entity.state
                arrays = {
                    "node_positions_m": deformable_state.node_positions_m,
                    "node_velocities_m_s": deformable_state.node_velocities_m_s,
                }
                tick = deformable_state.tick
            elif isinstance(entity, ParticleFluid):
                fluid_state = entity.state
                arrays = {
                    "particle_positions_m": fluid_state.particle_positions_m,
                    "particle_velocities_m_s": fluid_state.particle_velocities_m_s,
                }
                tick = fluid_state.tick
            elif isinstance(entity, Camera):
                sample = entity.sample()
                arrays = {channel.modality.value: channel.data for channel in sample.channels}
                tick = sample.tick
            else:  # pragma: no cover - all public EasyAPI entities are handled above
                raise ValueError("unsupported entity type")
            result: dict[str, Any] = {
                "session_id": owned.session_id,
                "path": entity.path.value,
                "kind": entity.kind.value,
                "configuration": owned.entities.get(entity.path.value, {}).get("configuration", {}),
                "tick": _tick_dict(tick),
                "state": {
                    name: _array_dict(
                        value, include_values=include_values, max_values=self.limits.max_observation_values
                    )
                    for name, value in arrays.items()
                },
            }
            if isinstance(entity, Articulation):
                result["joint_names"] = list(entity.joint_names)
            if include_contact:
                if not isinstance(entity, RigidBody):
                    raise ValueError("contact state is available only for rigid bodies")
                contact = entity.contact()
                result["contact"] = {
                    "net_normal_forces_n": _array_dict(
                        contact.net_normal_forces_n,
                        include_values=include_values,
                        max_values=self.limits.max_observation_values,
                    ),
                    "in_contact": _array_dict(
                        contact.in_contact,
                        include_values=include_values,
                        max_values=self.limits.max_observation_values,
                    ),
                    "tick": _tick_dict(contact.tick),
                }
            return result

    def scene_snapshot(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            owned = self._owned(session_id)
            self._running(owned)
            return {"session_id": owned.session_id, "snapshot": owned.sim.scene_snapshot().to_dict()}

    def capture_camera(
        self,
        session_id: str,
        camera_name: str,
        *,
        environment_index: int = 0,
        save_to_evidence: bool = False,
        filename: str | None = None,
    ) -> Screenshot:
        if not isinstance(save_to_evidence, bool):
            raise ValueError("save_to_evidence must be boolean")
        with self._lock:
            owned = self._owned(session_id)
            self._running(owned)
            entity = owned.sim.entity(camera_name)
            if not isinstance(entity, Camera):
                raise ValueError("target is not a camera")
            png, width, height = encode_rgb_png(entity.read("rgb"), environment_index)
            if width * height > self.limits.max_camera_pixels:
                raise ControlAccessError("camera frame exceeds max_camera_pixels")
            digest = hashlib.sha256(png).hexdigest()
            metadata_value: dict[str, Any] = {
                "session_id": owned.session_id,
                "camera_path": entity.path.value,
                "environment_index": environment_index,
                "width": width,
                "height": height,
                "format": "png",
                "sha256": digest,
                "source": "backend-camera-rgb",
                "saved_path": None,
            }
            if save_to_evidence:
                output_name = filename or f"{owned.session_id}-{entity.path.name}-env{environment_index}.png"
                if Path(output_name).name != output_name or not output_name.lower().endswith(".png"):
                    raise ControlAccessError("filename must be a basename ending in .png")
                screenshots = self.evidence_root / "screenshots"
                screenshots.mkdir(parents=True, exist_ok=True)
                output = screenshots / output_name
                output.write_bytes(png)
                metadata_value["saved_path"] = str(output.relative_to(self.evidence_root))
            return Screenshot(png, metadata_value)

    def close(self, session_id: str, lease_id: str, command_id: str) -> dict[str, Any]:
        def operation(owned: _OwnedSession) -> dict[str, Any]:
            owned.sim.close()
            owned.phase = "closed"
            return {"session_id": owned.session_id, "phase": owned.phase}

        return self._execute(
            session_id, lease_id, command_id, "simulation_close", {}, operation, allow_closed_replay=True
        )

    def close_all(self) -> None:
        with self._lock:
            for owned in self._sessions.values():
                if owned.phase != "closed":
                    owned.sim.close()
                    owned.phase = "closed"

    def _execute(
        self,
        session_id: str,
        lease_id: str,
        command_id: str,
        operation_name: str,
        payload: dict[str, Any],
        operation: Callable[[_OwnedSession], dict[str, Any]],
        *,
        allow_closed_replay: bool = False,
    ) -> dict[str, Any]:
        self._validate_identifier(command_id, "command_id")
        digest = hashlib.sha256(
            json.dumps(
                {"operation": operation_name, "payload": payload}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self._lock:
            owned = self._owned(session_id, reap=False)
            if owned.lease_id != lease_id:
                raise ControlAccessError("invalid session lease")
            previous = owned.commands.get(command_id)
            if previous is not None:
                if previous.digest != digest:
                    raise ControlAccessError("command_id was already used with different input")
                replay = dict(previous.result)
                replay["idempotent_replay"] = True
                return replay
            if owned.phase == "closed" and not allow_closed_replay:
                raise ControlAccessError("simulation session is closed")
            if self._clock() >= owned.expires_at:
                owned.sim.close()
                owned.phase = "closed"
                self._audit(operation_name, session_id, command_id, "rejected", {"reason": "lease_expired"})
                raise ControlAccessError("session lease has expired")
            try:
                result = operation(owned)
            except Exception as exc:
                self._audit(operation_name, session_id, command_id, "rejected", {"error": type(exc).__name__})
                raise
            owned.expires_at = self._clock() + self.limits.lease_timeout_seconds
            final = {**result, "command_id": command_id, "idempotent_replay": False}
            owned.commands[command_id] = _CommandRecord(digest, final)
            while len(owned.commands) > self.limits.max_cached_commands:
                owned.commands.popitem(last=False)
            self._audit(operation_name, session_id, command_id, "applied", {})
            return final

    def _owned(self, session_id: str, *, reap: bool = True) -> _OwnedSession:
        self._validate_identifier(session_id, "session_id")
        if reap:
            self._reap_expired()
        owned = self._sessions.get(session_id)
        if owned is None:
            raise ControlAccessError("unknown or expired simulation session")
        return owned

    def _reap_expired(self) -> None:
        now = self._clock()
        for session_id, owned in tuple(self._sessions.items()):
            if owned.phase != "closed" and now >= owned.expires_at:
                owned.sim.close()
                owned.phase = "closed"
                self._audit("lease_expired", session_id, None, "closed", {})
            if owned.phase == "closed" and now >= owned.expires_at:
                del self._sessions[session_id]

    @staticmethod
    def _running(owned: _OwnedSession) -> None:
        if owned.phase != "running":
            raise ControlAccessError("simulation session is not running")

    @staticmethod
    def _validate_identifier(value: str, name: str) -> None:
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{name} is invalid")

    @staticmethod
    def _required_text(value: dict[str, Any], name: str) -> str:
        result = value.get(name)
        if not isinstance(result, str) or not result:
            raise ValueError(f"{name} must be a non-empty string")
        return result

    @staticmethod
    def _entity_path(name: str) -> str:
        return name if name.startswith("/") else f"/{name}"

    def _check_points(self, value: object, name: str) -> None:
        try:
            array = ArrayValue.from_nested(value)
        except Exception as exc:
            raise ValueError(f"{name} must be a rectangular point array") from exc
        if len(array.shape) != 2 or array.shape[1] != 3:
            raise ValueError(f"{name} must have shape [point,3]")
        if array.shape[0] > self.limits.max_points_per_entity:
            raise ControlAccessError(f"{name} exceeds max_points_per_entity")

    def _asset_uri(self, entity: dict[str, Any]) -> str:
        value = self._optional_asset_uri(entity)
        if value is None:
            raise ValueError("asset_uri is required")
        return value

    def _optional_asset_uri(self, entity: dict[str, Any]) -> str | None:
        value = entity.get("asset_uri")
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("asset_uri must be a non-empty string")
        parsed = urlparse(value)
        if parsed.scheme not in {"", "file"}:
            raise ControlAccessError("MCP control accepts only allowlisted local asset files")
        candidate = Path(unquote(parsed.path) if parsed.scheme == "file" else value).resolve(strict=True)
        if not any(candidate.is_relative_to(root) for root in self.asset_roots):
            raise ControlAccessError("asset_uri is outside the configured asset roots")
        if not candidate.is_file():
            raise ControlAccessError("asset_uri must identify a file")
        return str(candidate)

    def _audit(
        self,
        operation: str,
        session_id: str,
        command_id: str | None,
        status: str,
        details: dict[str, Any],
    ) -> None:
        record = {
            "schema": "unirobosim.mcp.audit/v1",
            "timestamp_unix_seconds": time.time(),
            "operation": operation,
            "session_id": session_id,
            "command_id": command_id,
            "status": status,
            "details": details,
        }
        with self._audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
