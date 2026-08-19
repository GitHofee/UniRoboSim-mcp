from __future__ import annotations

import os
from pathlib import Path

import pytest

from unirobosim_mcp import (
    EvidenceLimits,
    EvidenceStore,
    query_events,
    query_primitives,
    query_reports,
    summarize_trace,
)
from unirobosim_mcp.store import EvidenceAccessError


def test_store_lists_reads_and_validates_trace(evidence_root: Path) -> None:
    store = EvidenceStore(evidence_root)
    listing = store.list_files(pattern="*.json*")
    assert [item["path"] for item in listing["items"]] == [
        "result.json",
        "run/sample.urs-debug.jsonl",
    ]
    assert listing["truncated"] is False
    assert store.read_json("result.json")["value"] == {"status": "passed"}
    assert store.read_text("notes.md")["content"] == "evidence notes"
    trace = store.read_trace("run/sample.urs-debug.jsonl")
    summary = summarize_trace(trace)
    assert summary["run_id"] == "mcp-test"
    assert summary["event_count"] == 5
    assert summary["publish_count"] == 3
    assert summary["primitive_count"] == 3
    assert summary["active_count"] == 1
    assert summary["report_count"] == 4
    assert summary["accepted_count"] == 3
    assert summary["dropped_count"] == 1
    assert summary["drop_reasons"] == {"event_rate": 1}
    assert summary["layers"] == ["control", "planning"]
    assert summary["environment_indices"] == [0, 1]


def test_store_rejects_traversal_escape_types_sizes_and_bad_text(evidence_root: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("outside", encoding="utf-8")
    (evidence_root / "escape.txt").symlink_to(outside)
    (evidence_root / "blocked.bin").write_bytes(b"binary")
    (evidence_root / "bad.txt").write_bytes(b"\xff")
    store = EvidenceStore(evidence_root, limits=EvidenceLimits(max_text_return_bytes=4))
    for value in ("../outside.txt", str(outside), "escape.txt", "blocked.bin", "a\\b.txt", ""):
        with pytest.raises(EvidenceAccessError):
            store.resolve_file(value)
    with pytest.raises(EvidenceAccessError, match="response byte limit"):
        store.read_text("notes.md")
    roomy = EvidenceStore(evidence_root)
    with pytest.raises(EvidenceAccessError, match="UTF-8"):
        roomy.read_text("bad.txt")
    with pytest.raises(EvidenceAccessError, match="not valid JSON"):
        roomy.read_json("notes.md")
    with pytest.raises(EvidenceAccessError, match="not a UniRoboSim"):
        roomy.read_trace("result.json")


def test_store_enforces_listing_scan_and_result_limits(evidence_root: Path) -> None:
    store = EvidenceStore(evidence_root, limits=EvidenceLimits(max_results=1))
    listing = store.list_files()
    assert listing["count"] == 1 and listing["truncated"] is True
    scanned = EvidenceStore(evidence_root, limits=EvidenceLimits(max_scanned_files=1)).list_files()
    assert scanned["scanned_files"] == 1 and scanned["truncated"] is True
    for pattern in ("", "../*", "a\\b"):
        with pytest.raises(EvidenceAccessError, match="pattern"):
            store.list_files(pattern=pattern)


def test_store_constructor_and_limit_validation(evidence_root: Path, tmp_path: Path) -> None:
    with pytest.raises(EvidenceAccessError, match="positive"):
        EvidenceLimits(max_results=0)
    with pytest.raises(FileNotFoundError):
        EvidenceStore(tmp_path / "missing")
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(EvidenceAccessError, match="directory"):
        EvidenceStore(file_path)


def test_event_queries_filter_paginate_and_validate(evidence_root: Path) -> None:
    trace = EvidenceStore(evidence_root).read_trace("run/sample.urs-debug.jsonl")
    page = query_events(trace, limit=1)
    assert page["count"] == 1 and page["matching_count"] == 5 and page["next_sequence"] == 2
    assert page["items"][0]["primitive_keys"] == [["planning", "targets", "point"]]
    clears = query_events(trace, event_kind="clear", start_sequence=2)
    assert clears["items"] == [
        {
            "sequence": 3,
            "kind": "clear",
            "layer": "planning",
            "group": None,
            "primitive_id": None,
        }
    ]
    resets = query_events(trace, event_kind="reset")
    assert resets["items"] == [{"sequence": 5, "kind": "reset"}]
    for kwargs in ({"event_kind": "bad"}, {"start_sequence": 0}, {"limit": 2, "max_items": 1}):
        with pytest.raises(EvidenceAccessError):
            query_events(trace, **kwargs)  # type: ignore[arg-type]


def test_report_queries_include_fully_rejected_calls_and_validate(evidence_root: Path) -> None:
    trace = EvidenceStore(evidence_root).read_trace("run/sample.urs-debug.jsonl")
    page = query_reports(trace, limit=2)
    assert page["count"] == 2 and page["matching_count"] == 4
    assert page["next_report_sequence"] == 3
    dropped = query_reports(trace, only_dropped=True, drop_reason="event_rate")
    assert dropped["count"] == 1
    assert dropped["items"][0]["event_id"] == "rate-limited-event"
    assert dropped["items"][0]["accepted_count"] == 0
    assert dropped["items"][0]["drop_reasons"] == {"event_rate": 1}
    for kwargs in (
        {"start_report_sequence": 0},
        {"only_dropped": 1},
        {"drop_reason": ""},
        {"limit": 2, "max_items": 1},
    ):
        with pytest.raises(EvidenceAccessError):
            query_reports(trace, **kwargs)  # type: ignore[arg-type]


def test_primitive_queries_reconstruct_filter_and_bound_geometry(evidence_root: Path) -> None:
    trace = EvidenceStore(evidence_root).read_trace("run/sample.urs-debug.jsonl")
    at_two = query_primitives(trace, sequence=2, limit=1)
    assert at_two["active_count"] == 2
    assert at_two["matching_count"] == 2 and at_two["truncated"] is True
    control = query_primitives(
        trace,
        layer="control",
        group="commands",
        primitive_id="line",
        primitive_kind="line_list",
        environment_index=1,
        include_geometry=True,
    )
    assert control["count"] == 1
    assert control["items"][0]["geometry_m"] == ((((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),),)
    final_state = query_primitives(trace)
    assert final_state["active_count"] == 1
    assert final_state["items"][0]["lifetime"] == {"mode": "manual"}
    assert query_primitives(trace, sequence=0)["active_count"] == 0
    invalid = (
        {"sequence": 6},
        {"primitive_kind": "mesh"},
        {"environment_index": -1},
        {"include_geometry": 1},
        {"layer": ""},
        {"limit": 0},
    )
    for kwargs in invalid:
        with pytest.raises(EvidenceAccessError):
            query_primitives(trace, **kwargs)  # type: ignore[arg-type]


def test_oversized_file_and_unavailable_file_are_rejected(evidence_root: Path) -> None:
    path = evidence_root / "large.log"
    path.write_text("12345", encoding="utf-8")
    store = EvidenceStore(evidence_root, limits=EvidenceLimits(max_file_bytes=4))
    with pytest.raises(EvidenceAccessError, match="byte limit"):
        store.resolve_file("large.log")
    os.unlink(path)
    with pytest.raises(EvidenceAccessError, match="unavailable"):
        store.resolve_file("large.log")
