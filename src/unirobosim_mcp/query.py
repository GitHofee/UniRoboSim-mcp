"""Pure trace summaries and bounded queries used by MCP and direct Python callers."""

from __future__ import annotations

from typing import Any

from unirobosim import (
    DebugLifetimeMode,
    DebugPrimitive,
    DebugPrimitiveKind,
    DebugTrace,
    DebugTraceEventKind,
)

from .store import EvidenceAccessError

DebugKey = tuple[str, str, str]


def summarize_trace(trace: DebugTrace) -> dict[str, Any]:
    manifest = trace.manifest
    return {
        "schema_version": manifest.schema_version,
        "run_id": manifest.run_id,
        "metadata": manifest.metadata.to_dict(),
        "closed": manifest.closed,
        "event_count": manifest.event_count,
        "publish_count": manifest.publish_count,
        "primitive_count": manifest.primitive_count,
        "active_count": manifest.active_count,
        "report_count": manifest.report_count,
        "accepted_count": manifest.accepted_count,
        "dropped_count": manifest.dropped_count,
        "filtered_count": manifest.filtered_count,
        "drop_reasons": manifest.drop_reasons.to_dict(),
        "layers": list(manifest.layers),
        "groups": list(manifest.groups),
        "environment_indices": list(manifest.environment_indices),
        "first_step_index": manifest.first_step_index,
        "last_step_index": manifest.last_step_index,
    }


def _bounded_limit(limit: int, maximum: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise EvidenceAccessError(f"query limit must be between 1 and {maximum}")
    return limit


def _optional_text(value: str | None, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256):
        raise EvidenceAccessError(f"{name} filter is invalid")
    return value


def query_events(
    trace: DebugTrace,
    *,
    event_kind: str | None = None,
    start_sequence: int = 1,
    limit: int = 100,
    max_items: int = 1_000,
) -> dict[str, Any]:
    count = _bounded_limit(limit, max_items)
    if not isinstance(start_sequence, int) or isinstance(start_sequence, bool) or start_sequence <= 0:
        raise EvidenceAccessError("start_sequence must be a positive integer")
    kind: DebugTraceEventKind | None = None
    if event_kind is not None:
        try:
            kind = DebugTraceEventKind(event_kind)
        except (TypeError, ValueError) as exc:
            raise EvidenceAccessError("event_kind is unsupported") from exc
    matching = [
        event for event in trace.events if event.sequence >= start_sequence and (kind is None or event.kind is kind)
    ]
    items: list[dict[str, Any]] = []
    for event in matching[:count]:
        item: dict[str, Any] = {"sequence": event.sequence, "kind": event.kind.value}
        if event.kind is DebugTraceEventKind.PUBLISH:
            assert event.batch is not None
            item.update(
                {
                    "event_id": event.batch.event_id,
                    "step_index": event.batch.step_index,
                    "sim_time_s": event.batch.sim_time_s,
                    "world_generation": event.batch.world_generation,
                    "primitive_count": len(event.batch.primitives),
                    "primitive_keys": [list(primitive.key) for primitive in event.batch.primitives],
                    "primitive_kinds": [primitive.kind.value for primitive in event.batch.primitives],
                }
            )
        elif event.kind is DebugTraceEventKind.CLEAR:
            item.update(
                {
                    "layer": event.layer,
                    "group": event.group,
                    "primitive_id": event.primitive_id,
                }
            )
        items.append(item)
    return {
        "items": items,
        "count": len(items),
        "matching_count": len(matching),
        "truncated": len(matching) > count,
        "next_sequence": items[-1]["sequence"] + 1 if len(matching) > count else None,
    }


def query_reports(
    trace: DebugTrace,
    *,
    start_report_sequence: int = 1,
    only_dropped: bool = False,
    drop_reason: str | None = None,
    limit: int = 100,
    max_items: int = 1_000,
) -> dict[str, Any]:
    """Return bounded publish-decision evidence, including fully rejected calls."""

    count = _bounded_limit(limit, max_items)
    if (
        not isinstance(start_report_sequence, int)
        or isinstance(start_report_sequence, bool)
        or start_report_sequence <= 0
    ):
        raise EvidenceAccessError("start_report_sequence must be a positive integer")
    if not isinstance(only_dropped, bool):
        raise EvidenceAccessError("only_dropped must be boolean")
    reason = _optional_text(drop_reason, "drop_reason")
    matching = [
        report
        for report in trace.reports
        if report.report_sequence >= start_report_sequence
        and (not only_dropped or report.dropped_count > 0)
        and (reason is None or reason in report.drop_reasons)
    ]
    items = [
        {
            "report_sequence": report.report_sequence,
            "event_id": report.event_id,
            "step_index": report.step_index,
            "sim_time_s": report.sim_time_s,
            "world_generation": report.world_generation,
            "requested_count": report.requested_count,
            "accepted_count": report.accepted_count,
            "dropped_count": report.dropped_count,
            "filtered_count": report.filtered_count,
            "active_count": report.active_count,
            "elapsed_ms": report.elapsed_ms,
            "budget_exceeded": report.budget_exceeded,
            "drop_reasons": report.drop_reasons.to_dict(),
            "sink_failures": list(report.sink_failures),
        }
        for report in matching[:count]
    ]
    next_report_sequence: int | None = None
    if len(matching) > count:
        next_report_sequence = matching[count - 1].report_sequence + 1
    return {
        "items": items,
        "count": len(items),
        "matching_count": len(matching),
        "truncated": len(matching) > count,
        "next_report_sequence": next_report_sequence,
    }


def _matches(
    primitive: DebugPrimitive,
    *,
    layer: str | None,
    group: str | None,
    primitive_id: str | None,
    kind: DebugPrimitiveKind | None,
    environment_index: int | None,
) -> bool:
    return (
        (layer is None or primitive.layer == layer)
        and (group is None or primitive.group == group)
        and (primitive_id is None or primitive.primitive_id == primitive_id)
        and (kind is None or primitive.kind is kind)
        and (environment_index is None or environment_index in primitive.environment_indices)
    )


def _snapshot(trace: DebugTrace, sequence: int) -> dict[DebugKey, DebugPrimitive]:
    active: dict[DebugKey, DebugPrimitive] = {}
    for event in trace.events:
        if event.sequence > sequence:
            break
        if event.kind is DebugTraceEventKind.PUBLISH:
            assert event.batch is not None
            for primitive in event.batch.primitives:
                active[primitive.key] = primitive
        elif event.kind is DebugTraceEventKind.CLEAR:
            for key in tuple(active):
                if (
                    (event.layer is None or key[0] == event.layer)
                    and (event.group is None or key[1] == event.group)
                    and (event.primitive_id is None or key[2] == event.primitive_id)
                ):
                    active.pop(key)
        else:
            for key, primitive in tuple(active.items()):
                if primitive.lifetime.mode is not DebugLifetimeMode.MANUAL:
                    active.pop(key)
    return active


def _primitive_record(primitive: DebugPrimitive, *, include_geometry: bool) -> dict[str, Any]:
    if include_geometry:
        return primitive.to_dict()
    return {
        "primitive_id": primitive.primitive_id,
        "layer": primitive.layer,
        "group": primitive.group,
        "source": primitive.source,
        "kind": primitive.kind.value,
        "environment_indices": list(primitive.environment_indices),
        "color_rgba": list(primitive.color_rgba),
        "size": primitive.size,
        "lifetime": primitive.lifetime.to_dict(),
        "vertex_count": primitive.vertex_count,
        "estimated_payload_bytes": primitive.estimated_payload_bytes,
        "geometry_shape": list(primitive.geometry_m.shape),
    }


def query_primitives(
    trace: DebugTrace,
    *,
    sequence: int | None = None,
    layer: str | None = None,
    group: str | None = None,
    primitive_id: str | None = None,
    primitive_kind: str | None = None,
    environment_index: int | None = None,
    include_geometry: bool = False,
    limit: int = 100,
    max_items: int = 1_000,
) -> dict[str, Any]:
    count = _bounded_limit(limit, max_items)
    layer = _optional_text(layer, "layer")
    group = _optional_text(group, "group")
    primitive_id = _optional_text(primitive_id, "primitive_id")
    if not isinstance(include_geometry, bool):
        raise EvidenceAccessError("include_geometry must be boolean")
    if environment_index is not None and (
        not isinstance(environment_index, int) or isinstance(environment_index, bool) or environment_index < 0
    ):
        raise EvidenceAccessError("environment_index must be a non-negative integer")
    kind: DebugPrimitiveKind | None = None
    if primitive_kind is not None:
        try:
            kind = DebugPrimitiveKind(primitive_kind)
        except (TypeError, ValueError) as exc:
            raise EvidenceAccessError("primitive_kind is unsupported") from exc
    maximum_sequence = trace.events[-1].sequence if trace.events else 0
    selected_sequence = maximum_sequence if sequence is None else sequence
    if (
        not isinstance(selected_sequence, int)
        or isinstance(selected_sequence, bool)
        or not 0 <= selected_sequence <= maximum_sequence
    ):
        raise EvidenceAccessError(f"sequence must be between 0 and {maximum_sequence}")
    active = _snapshot(trace, selected_sequence)
    matching = [
        primitive
        for key, primitive in sorted(active.items())
        if _matches(
            primitive,
            layer=layer,
            group=group,
            primitive_id=primitive_id,
            kind=kind,
            environment_index=environment_index,
        )
    ]
    return {
        "sequence": selected_sequence,
        "active_count": len(active),
        "items": [_primitive_record(primitive, include_geometry=include_geometry) for primitive in matching[:count]],
        "count": min(len(matching), count),
        "matching_count": len(matching),
        "truncated": len(matching) > count,
    }
