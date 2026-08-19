from __future__ import annotations

import json
from pathlib import Path

import pytest
from unirobosim import (
    ArrayValue,
    DebugBatch,
    DebugBudget,
    DebugBus,
    DebugLifetime,
    DebugPrimitive,
    DebugPrimitiveKind,
    TraceDebugSink,
)


@pytest.fixture
def evidence_root(tmp_path: Path) -> Path:
    trace_path = tmp_path / "run" / "sample.urs-debug.jsonl"
    sink = TraceDebugSink(trace_path, run_id="mcp-test")
    bus = DebugBus((sink,), budget=DebugBudget(max_events_per_second=3))
    point = DebugPrimitive(
        "point",
        "planning",
        DebugPrimitiveKind.POINT_SET,
        ArrayValue.from_nested([[[1.0, 2.0, 3.0]]]),
        (0,),
        group="targets",
        lifetime=DebugLifetime.persistent(),
    )
    line = DebugPrimitive(
        "line",
        "control",
        DebugPrimitiveKind.LINE_LIST,
        ArrayValue.from_nested([[[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]]]),
        (1,),
        group="commands",
        lifetime=DebugLifetime.manual(),
    )
    bus.publish(DebugBatch((point,), step_index=1, sim_time_s=0.1, event_id="point-event"))
    bus.publish(DebugBatch((line,), step_index=2, sim_time_s=0.2, event_id="line-event"))
    assert bus.clear(layer="planning") == 1
    reset_point = DebugPrimitive(
        "reset-point",
        "planning",
        DebugPrimitiveKind.POINT_SET,
        ArrayValue.from_nested([[[4.0, 5.0, 6.0]]]),
        (0,),
        group="targets",
        lifetime=DebugLifetime.persistent(),
    )
    bus.publish(DebugBatch((reset_point,), step_index=3, sim_time_s=0.3, event_id="reset-event"))
    assert bus.reset() == 1
    rejected = bus.publish(DebugBatch((point,), step_index=4, sim_time_s=0.4, event_id="rate-limited-event"))
    assert rejected.accepted_count == 0 and rejected.drop_reasons["event_rate"] == 1
    bus.close()
    (tmp_path / "result.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    (tmp_path / "notes.md").write_text("evidence notes", encoding="utf-8")
    return tmp_path
