"""Bounded evidence-root access with no simulator or network dependency."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from unirobosim import DebugTrace, DebugTraceReader

_TRACE_SUFFIX = ".urs-debug.jsonl"
_TEXT_SUFFIXES = (_TRACE_SUFFIX, ".json", ".svg", ".html", ".md", ".txt", ".log")


class EvidenceAccessError(ValueError):
    """A bounded evidence access request is invalid or unsafe."""


@dataclass(frozen=True)
class EvidenceLimits:
    max_file_bytes: int = 64 * 1024 * 1024
    max_text_return_bytes: int = 2 * 1024 * 1024
    max_trace_events: int = 1_000_000
    max_results: int = 500
    max_query_items: int = 1_000
    max_scanned_files: int = 20_000

    def __post_init__(self) -> None:
        values = (
            self.max_file_bytes,
            self.max_text_return_bytes,
            self.max_trace_events,
            self.max_results,
            self.max_query_items,
            self.max_scanned_files,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
            raise EvidenceAccessError("evidence limits must be positive integers")


class EvidenceStore:
    """Allowlisted read-only view of one explicit evidence directory."""

    def __init__(self, root: str | Path, *, limits: EvidenceLimits | None = None) -> None:
        self._root = Path(root).resolve(strict=True)
        if not self._root.is_dir():
            raise EvidenceAccessError("evidence root must be an existing directory")
        self._limits = limits or EvidenceLimits()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def limits(self) -> EvidenceLimits:
        return self._limits

    def resolve_file(self, relative_path: str, *, trace_only: bool = False) -> Path:
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or len(relative_path.encode("utf-8")) > 4096
            or "\x00" in relative_path
            or "\\" in relative_path
        ):
            raise EvidenceAccessError("evidence path is invalid")
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
            raise EvidenceAccessError("evidence path must be relative and cannot traverse")
        try:
            candidate = self._root.joinpath(*pure.parts).resolve(strict=True)
        except OSError as exc:
            raise EvidenceAccessError("evidence file is unavailable") from exc
        if not candidate.is_relative_to(self._root) or not candidate.is_file():
            raise EvidenceAccessError("evidence path escapes the configured root or is not a file")
        if trace_only:
            if not candidate.name.endswith(_TRACE_SUFFIX):
                raise EvidenceAccessError("the requested evidence is not a UniRoboSim debug trace")
        elif not candidate.name.endswith(_TEXT_SUFFIXES):
            raise EvidenceAccessError("evidence file type is not allowlisted")
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise EvidenceAccessError("evidence file metadata is unavailable") from exc
        if size > self._limits.max_file_bytes:
            raise EvidenceAccessError("evidence file exceeds the configured byte limit")
        return candidate

    def list_files(self, *, pattern: str = "*") -> dict[str, Any]:
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern.encode("utf-8")) > 256
            or "\x00" in pattern
            or "\\" in pattern
            or ".." in PurePosixPath(pattern).parts
        ):
            raise EvidenceAccessError("evidence pattern is invalid")
        entries: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        for directory, names, files in os.walk(self._root, followlinks=False):
            names[:] = sorted(name for name in names if not Path(directory, name).is_symlink())
            for name in sorted(files):
                scanned += 1
                if scanned > self._limits.max_scanned_files:
                    truncated = True
                    break
                candidate = Path(directory, name)
                if not candidate.name.endswith(_TEXT_SUFFIXES):
                    continue
                relative = candidate.relative_to(self._root).as_posix()
                if not PurePosixPath(relative).match(pattern):
                    continue
                try:
                    resolved = self.resolve_file(relative)
                    stat = resolved.stat()
                except (EvidenceAccessError, OSError):
                    continue
                entries.append(
                    {
                        "path": relative,
                        "kind": "debug_trace" if relative.endswith(_TRACE_SUFFIX) else resolved.suffix.lstrip("."),
                        "size_bytes": stat.st_size,
                        "modified_ns": stat.st_mtime_ns,
                    }
                )
                if len(entries) >= self._limits.max_results:
                    truncated = True
                    break
            if truncated:
                break
        return {
            "root": str(self._root),
            "pattern": pattern,
            "items": entries,
            "count": len(entries),
            "scanned_files": min(scanned, self._limits.max_scanned_files),
            "truncated": truncated,
        }

    def read_text(self, relative_path: str) -> dict[str, Any]:
        path = self.resolve_file(relative_path)
        if path.stat().st_size > self._limits.max_text_return_bytes:
            raise EvidenceAccessError("evidence text exceeds the configured response byte limit")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise EvidenceAccessError("evidence file is not valid bounded UTF-8 text") from exc
        return {
            "path": relative_path,
            "size_bytes": path.stat().st_size,
            "content": content,
        }

    def read_json(self, relative_path: str) -> dict[str, Any]:
        record = self.read_text(relative_path)
        try:
            value = json.loads(record["content"])
        except json.JSONDecodeError as exc:
            raise EvidenceAccessError("evidence file is not valid JSON") from exc
        return {"path": relative_path, "size_bytes": record["size_bytes"], "value": value}

    def read_trace(self, relative_path: str) -> DebugTrace:
        path = self.resolve_file(relative_path, trace_only=True)
        return DebugTraceReader(
            max_bytes=self._limits.max_file_bytes,
            max_events=self._limits.max_trace_events,
        ).read(path)
