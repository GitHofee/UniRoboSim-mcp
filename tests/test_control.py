from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unirobosim_mcp.control import ControlAccessError, ControlLimits, SimulationControl, encode_rgb_png


def _create(control: SimulationControl, **kwargs: Any) -> tuple[str, str]:
    created = control.create(backend="fake", **kwargs)
    return created["session_id"], created["lease_id"]


def _configure_full_scene(control: SimulationControl, session_id: str, lease_id: str) -> None:
    entities = (
        (
            "box-add",
            {
                "kind": "box",
                "name": "test_body",
                "size_m": [0.2, 0.2, 0.2],
                "color_rgba": [0.2, 0.55, 0.9, 1.0],
                "position_m": [0.0, 0.0, 0.5],
            },
        ),
        (
            "door-add",
            {
                "kind": "articulation",
                "name": "door",
                "joint_names": ["hinge"],
                "initial_positions": [0.0],
            },
        ),
        (
            "camera-add",
            {"kind": "camera", "name": "camera", "resolution": [64, 48], "outputs": ["rgb", "depth"]},
        ),
        (
            "cloth-add",
            {
                "kind": "deformable",
                "name": "cloth",
                "rest_positions_m": [[0.0, 0.0, 1.0], [0.1, 0.0, 1.0], [0.0, 0.1, 1.0]],
                "surface_triangles": [[0, 1, 2]],
            },
        ),
        (
            "fluid-add",
            {
                "kind": "particle_fluid",
                "name": "water",
                "positions_m": [[0.0, 0.0, 0.5], [0.02, 0.0, 0.5]],
            },
        ),
    )
    for command_id, entity in entities:
        result = control.configure_entity(session_id, lease_id, command_id, entity)
        assert result["idempotent_replay"] is False


def test_full_control_surface_and_typed_reads(tmp_path: Path) -> None:
    control = SimulationControl(tmp_path, limits=ControlLimits(max_observation_values=4))
    info = control.info()
    assert info["mutation_requires_lease"] is True
    assert info["read_requires_lease"] is False
    assert any(item["backend"] == "fake" and item["available"] for item in control.list_backends()["backends"])

    session_id, lease_id = _create(control, num_envs=2)
    assert control.list_sessions()["sessions"][0]["phase"] == "configuring"
    _configure_full_scene(control, session_id, lease_id)
    started = control.start(session_id, lease_id, "start")
    assert started["build_report"]["entity_count"] == 5
    assert control.renew_lease(session_id, lease_id, "renew")["lease_timeout_seconds"] == 300.0

    assert control.command(
        session_id,
        lease_id,
        "door-position",
        {"kind": "articulation", "entity": "door", "targets": [0.4]},
    )["accepted"]
    assert control.command(
        session_id,
        lease_id,
        "box-force",
        {"kind": "rigid_wrench", "entity": "test_body", "force_n": [1.0, 0.0, 0.0]},
    )["accepted"]
    assert control.command(
        session_id,
        lease_id,
        "cloth-position",
        {
            "kind": "deformable",
            "entity": "cloth",
            "mode": "position",
            "nodes": [0],
            "targets": [[0.0, 0.0, 1.1]],
        },
    )["accepted"]
    assert control.command(
        session_id,
        lease_id,
        "fluid-force",
        {
            "kind": "particle_fluid",
            "entity": "water",
            "mode": "force",
            "particles": [0],
            "targets": [[0.0, 0.0, 0.1]],
        },
    )["accepted"]
    assert (
        control.command(
            session_id,
            lease_id,
            "set-pose",
            {
                "kind": "scene",
                "scene_kind": "set_pose",
                "entity": "test_body",
                "target_pose": {"position_m": [0.1, 0.2, 0.6], "orientation_xyzw": [0.0, 0.0, 0.0, 1.0]},
            },
        )["result"]["status"]
        == "applied"
    )
    assert (
        control.command(
            session_id,
            lease_id,
            "clear-debug",
            {"kind": "debug_clear", "layer": "planning"},
        )["cleared"]
        == 0
    )

    stepped = control.step(session_id, lease_id, "step", 2)
    assert stepped["tick"]["step_index"] == 2
    replay = control.step(session_id, lease_id, "step", 2)
    assert replay["idempotent_replay"] is True

    rigid = control.get_entity(session_id, "test_body", include_values=True, include_contact=True)
    assert rigid["kind"] == "rigid_body"
    assert rigid["state"]["positions_m"]["values_complete"] is False
    articulation = control.get_entity(session_id, "/door", include_values=True)
    assert articulation["joint_names"] == ["hinge"]
    assert articulation["state"]["joint_positions"]["values"] == [0.4, 0.4]
    assert "node_positions_m" in control.get_entity(session_id, "cloth")["state"]
    assert "particle_positions_m" in control.get_entity(session_id, "water")["state"]
    assert set(control.get_entity(session_id, "camera")["state"]) == {"rgb", "depth"}

    snapshot = control.scene_snapshot(session_id)["snapshot"]
    assert {item["path"] for item in snapshot["entities"]} == {
        "/camera",
        "/cloth",
        "/door",
        "/test_body",
        "/water",
    }
    screenshot = control.capture_camera(
        session_id,
        "camera",
        environment_index=1,
        save_to_evidence=True,
        filename="agent-camera.png",
    )
    assert screenshot.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert screenshot.metadata["source"] == "backend-camera-rgb"
    assert (tmp_path / screenshot.metadata["saved_path"]).read_bytes() == screenshot.png
    with pytest.raises(ControlAccessError, match="basename"):
        control.capture_camera(
            session_id,
            "camera",
            save_to_evidence=True,
            filename="../bad.png",
        )

    reset = control.reset(session_id, lease_id, "reset", [1])
    assert reset["environment_indices"] == [1]
    assert control.close(session_id, lease_id, "close")["phase"] == "closed"
    assert control.close(session_id, lease_id, "close")["idempotent_replay"] is True
    control.close_all()

    audit = [json.loads(line) for line in (tmp_path / "mcp-control-audit.jsonl").read_text().splitlines()]
    assert {record["operation"] for record in audit} >= {
        "simulation_create",
        "simulation_configure_entity",
        "simulation_start",
        "simulation_command",
        "simulation_step",
        "simulation_reset",
        "simulation_close",
    }
    assert all("lease_id" not in record for record in audit)


def test_lease_idempotency_limits_and_input_boundaries(tmp_path: Path) -> None:
    now = [10.0]
    limits = ControlLimits(
        max_sessions=1,
        max_entities_per_session=1,
        max_environments=2,
        max_steps_per_call=3,
        max_points_per_entity=2,
        max_camera_pixels=100,
        max_cached_commands=1,
        lease_timeout_seconds=2.0,
    )
    control = SimulationControl(tmp_path, limits=limits, clock=lambda: now[0])
    session_id, lease_id = _create(control)
    with pytest.raises(ControlAccessError, match="maximum owned"):
        _create(control)
    with pytest.raises(ControlAccessError, match="invalid session lease"):
        control.configure_entity(session_id, "wrong", "add", {"kind": "box", "name": "box"})
    added = control.configure_entity(session_id, lease_id, "add", {"kind": "box", "name": "box"})
    assert added["path"] == "/box"
    with pytest.raises(ControlAccessError, match="different input"):
        control.configure_entity(session_id, lease_id, "add", {"kind": "box", "name": "other"})
    with pytest.raises(ControlAccessError, match="maximum entities"):
        control.configure_entity(session_id, lease_id, "other", {"kind": "box", "name": "other"})
    control.start(session_id, lease_id, "start")
    with pytest.raises(ValueError, match="between 1 and 3"):
        control.step(session_id, lease_id, "too-many", 4)
    now[0] = 13.0
    with pytest.raises(ControlAccessError, match="expired"):
        control.get_entity(session_id, "box")
    assert control.list_sessions()["sessions"] == []

    with pytest.raises(ValueError, match="between 1 and 2"):
        control.create(backend="fake", num_envs=3)


def test_asset_allowlist_and_entity_validation(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    model = assets / "model.urdf"
    model.write_text("<robot name='fixture'/>", encoding="utf-8")
    outside = tmp_path / "outside.urdf"
    outside.write_text("<robot name='outside'/>", encoding="utf-8")
    control = SimulationControl(tmp_path / "evidence", asset_roots=(assets,))
    session_id, lease_id = _create(control)
    result = control.configure_entity(
        session_id,
        lease_id,
        "asset-articulation",
        {"kind": "articulation", "name": "fixture", "joint_names": ["joint"], "asset_uri": str(model)},
    )
    assert result["path"] == "/fixture"
    with pytest.raises(ControlAccessError, match="outside"):
        control.configure_entity(
            session_id,
            lease_id,
            "outside",
            {"kind": "rigid_body", "name": "outside", "asset_uri": str(outside)},
        )
    with pytest.raises(ControlAccessError, match="only allowlisted local"):
        control.configure_entity(
            session_id,
            lease_id,
            "remote",
            {"kind": "rigid_body", "name": "remote", "asset_uri": "https://example.com/model.usd"},
        )
    with pytest.raises(ValueError, match="entity kind"):
        control.configure_entity(session_id, lease_id, "unknown", {"kind": "light", "name": "light"})


def test_png_encoder_rejects_invalid_data() -> None:
    from unirobosim import ArrayValue

    png, width, height = encode_rgb_png(
        ArrayValue((1, 1, 2, 3), (255, 0, 0, 0, 255, 0), dtype="uint8"),
        0,
    )
    assert (width, height) == (2, 1)
    assert png.startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="uint8 shape"):
        encode_rgb_png(ArrayValue((1, 1, 1), (0.0,), dtype="float32"), 0)
    with pytest.raises(ValueError, match="outside"):
        encode_rgb_png(ArrayValue((1, 1, 1, 3), (0, 0, 0), dtype="uint8"), 1)
    with pytest.raises(ValueError, match="integer"):
        encode_rgb_png(ArrayValue((1, 1, 1, 3), (0, 0, 0), dtype="uint8"), True)


def test_control_limit_and_request_validation(tmp_path: Path) -> None:
    for kwargs in ({"max_sessions": 0}, {"max_sessions": True}, {"lease_timeout_seconds": 0.0}):
        with pytest.raises(ValueError):
            ControlLimits(**kwargs)

    control = SimulationControl(
        tmp_path,
        limits=ControlLimits(max_camera_pixels=64, max_points_per_entity=2),
    )
    with pytest.raises(ValueError, match="backend"):
        control.create(backend="")
    session_id, lease_id = _create(control)
    with pytest.raises(ValueError, match="entity must"):
        control.configure_entity(session_id, lease_id, "object", [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        control.configure_entity(session_id, lease_id, "missing-kind", {"name": "missing"})
    with pytest.raises(ValueError, match="resolution"):
        control.configure_entity(
            session_id,
            lease_id,
            "bad-resolution",
            {"kind": "camera", "name": "camera", "resolution": [32.0, 32]},
        )
    with pytest.raises(ControlAccessError, match="max_camera_pixels"):
        control.configure_entity(
            session_id,
            lease_id,
            "large-camera",
            {"kind": "camera", "name": "camera", "resolution": [9, 8]},
        )
    with pytest.raises(ValueError, match="rectangular"):
        control.configure_entity(
            session_id,
            lease_id,
            "bad-points",
            {"kind": "deformable", "name": "cloth", "rest_positions_m": None},
        )
    with pytest.raises(ValueError, match=r"shape \[point,3\]"):
        control.configure_entity(
            session_id,
            lease_id,
            "bad-shape",
            {"kind": "deformable", "name": "cloth", "rest_positions_m": [[0.0, 0.0]]},
        )
    with pytest.raises(ControlAccessError, match="max_points"):
        control.configure_entity(
            session_id,
            lease_id,
            "too-many-points",
            {
                "kind": "particle_fluid",
                "name": "water",
                "positions_m": [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.2, 0.0, 0.0]],
            },
        )
    control.configure_entity(session_id, lease_id, "box", {"kind": "box", "name": "box"})
    with pytest.raises(ControlAccessError, match="not running"):
        control.get_entity(session_id, "box")
    control.start(session_id, lease_id, "start")
    with pytest.raises(ControlAccessError, match="before simulation_start"):
        control.configure_entity(session_id, lease_id, "late", {"kind": "box", "name": "late"})
    with pytest.raises(ControlAccessError, match="not configuring"):
        control.start(session_id, lease_id, "second-start")
    with pytest.raises(ValueError, match="command must"):
        control.command(session_id, lease_id, "bad-command", [])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported"):
        control.command(session_id, lease_id, "unsupported", {"kind": "teleport"})
    with pytest.raises(ValueError, match="articulation"):
        control.command(
            session_id,
            lease_id,
            "wrong-articulation",
            {"kind": "articulation", "entity": "box", "targets": [0.0]},
        )
    with pytest.raises(ValueError, match="boolean"):
        control.get_entity(session_id, "box", include_values=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="camera"):
        control.capture_camera(session_id, "box")
    with pytest.raises(ValueError, match="boolean"):
        control.capture_camera(session_id, "box", save_to_evidence=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid"):
        control.step(session_id, lease_id, "bad command id!", 1)
    control.close(session_id, lease_id, "close")
    with pytest.raises(ControlAccessError, match="closed"):
        control.step(session_id, lease_id, "after-close", 1)


def test_scene_drag_transactions_and_command_target_errors(tmp_path: Path) -> None:
    control = SimulationControl(tmp_path)
    session_id, lease_id = _create(control)
    _configure_full_scene(control, session_id, lease_id)
    control.start(session_id, lease_id, "start")

    begin = control.command(
        session_id,
        lease_id,
        "drag-begin-command",
        {
            "kind": "scene",
            "scene_kind": "drag_begin",
            "entity": "test_body",
            "drag_id": "drag-1",
            "drag_mode": "kinematic",
            "grab_point_world_m": [0.0, 0.0, 0.5],
        },
    )
    assert begin["result"]["status"] == "applied"
    update = control.command(
        session_id,
        lease_id,
        "drag-update-command",
        {
            "kind": "scene",
            "scene_kind": "drag_update",
            "entity": "/test_body",
            "drag_id": "drag-1",
            "target_pose": {"position_m": [0.2, 0.0, 0.7], "orientation_xyzw": [0.0, 0.0, 0.0, 1.0]},
        },
    )
    assert update["result"]["status"] == "applied"
    end = control.command(
        session_id,
        lease_id,
        "drag-end-command",
        {"kind": "scene", "scene_kind": "drag_end", "entity": "test_body", "drag_id": "drag-1"},
    )
    assert end["result"]["status"] == "applied"

    with pytest.raises(ValueError, match="target_pose"):
        control.command(
            session_id,
            lease_id,
            "bad-pose",
            {"kind": "scene", "scene_kind": "set_pose", "entity": "test_body", "target_pose": []},
        )
    for command_id, kind, entity in (
        ("wrong-rigid", "rigid_wrench", "door"),
        ("wrong-deformable", "deformable", "water"),
        ("wrong-fluid", "particle_fluid", "cloth"),
    ):
        payload: dict[str, Any] = {"kind": kind, "entity": entity, "targets": [[0.0, 0.0, 0.0]]}
        if kind == "rigid_wrench":
            payload = {"kind": kind, "entity": entity, "force_n": [0.0, 0.0, 0.0]}
        with pytest.raises(ValueError, match="target is not"):
            control.command(session_id, lease_id, command_id, payload)
    with pytest.raises(ValueError, match="contact state"):
        control.get_entity(session_id, "door", include_contact=True)


def test_expired_write_probe_failures_and_close_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = [0.0]
    control = SimulationControl(tmp_path, limits=ControlLimits(lease_timeout_seconds=1.0), clock=lambda: now[0])
    session_id, lease_id = _create(control)
    control.configure_entity(session_id, lease_id, "box", {"kind": "box", "name": "box"})
    now[0] = 2.0
    with pytest.raises(ControlAccessError, match="expired"):
        control.start(session_id, lease_id, "expired-start")

    other = SimulationControl(tmp_path / "other")
    _create(other)
    other.close_all()
    assert other.list_sessions()["sessions"][0]["phase"] == "closed"

    assert (
        SimulationControl._probe_provider("broken", lambda: (_ for _ in ()).throw(RuntimeError("boom")))["available"]
        is False
    )

    class BadEntryPoint:
        name = "broken-entry"

        @staticmethod
        def load() -> Any:
            raise RuntimeError("broken load")

    assert SimulationControl._probe_entry_point(BadEntryPoint())["available"] is False  # type: ignore[arg-type]

    class DuplicateEntryPoint:
        name = "fake"

    monkeypatch.setattr("unirobosim_mcp.control.metadata.entry_points", lambda **kwargs: (DuplicateEntryPoint(),))
    assert [item["backend"] for item in SimulationControl(tmp_path / "duplicate").list_backends()["backends"]] == [
        "fake"
    ]


def test_asset_missing_invalid_and_directory_rejections(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    control = SimulationControl(tmp_path / "evidence", asset_roots=(assets,))
    session_id, lease_id = _create(control)
    with pytest.raises(ValueError, match="required"):
        control.configure_entity(session_id, lease_id, "missing", {"kind": "rigid_body", "name": "body"})
    with pytest.raises(ValueError, match="non-empty"):
        control.configure_entity(
            session_id,
            lease_id,
            "invalid",
            {"kind": "rigid_body", "name": "body", "asset_uri": 1},
        )
    with pytest.raises(ControlAccessError, match="identify a file"):
        control.configure_entity(
            session_id,
            lease_id,
            "directory",
            {"kind": "rigid_body", "name": "body", "asset_uri": str(assets)},
        )
    model = assets / "model.obj"
    model.write_text("o fixture\n", encoding="utf-8")
    configured = control.configure_entity(
        session_id,
        lease_id,
        "file-uri",
        {"kind": "rigid_body", "name": "body", "asset_uri": model.as_uri()},
    )
    assert configured["path"] == "/body"
