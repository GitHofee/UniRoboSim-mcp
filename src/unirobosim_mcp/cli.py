"""Console entrypoint for stdio or loopback Streamable HTTP MCP transports."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .control import ControlLimits, SimulationControl
from .server import create_server

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve bounded UniRoboSim evidence and optional simulation control")
    parser.add_argument(
        "--root",
        type=Path,
        default=os.environ.get("UNIROBOSIM_EVIDENCE_ROOT"),
        help="allowlisted evidence root (or UNIROBOSIM_EVIDENCE_ROOT)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--enable-control",
        action="store_true",
        help="enable lease-protected control of sessions created by this server",
    )
    parser.add_argument(
        "--asset-root",
        type=Path,
        action="append",
        default=[],
        help="allow one local asset tree for control mode; repeat for multiple roots",
    )
    parser.add_argument("--max-sessions", type=int, default=2)
    parser.add_argument("--lease-timeout-seconds", type=float, default=300.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.root is None:
        parser.error("--root or UNIROBOSIM_EVIDENCE_ROOT is required")
    if args.transport == "streamable-http" and args.host not in _LOOPBACK_HOSTS:
        parser.error("unauthenticated HTTP transport is restricted to a loopback host")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.asset_root and not args.enable_control:
        parser.error("--asset-root requires --enable-control")
    try:
        control_limits = ControlLimits(
            max_sessions=args.max_sessions,
            lease_timeout_seconds=args.lease_timeout_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    control = (
        SimulationControl(args.root, asset_roots=tuple(args.asset_root), limits=control_limits)
        if args.enable_control
        else None
    )
    server = create_server(args.root, control=control)
    kwargs: dict[str, Any] = {}
    if args.transport == "streamable-http":
        settings = getattr(server, "settings", None)
        if settings is None:
            kwargs.update(host=args.host, port=args.port)
        else:
            settings.host = args.host
            settings.port = args.port
    try:
        server.run(transport=args.transport, **kwargs)
    finally:
        if control is not None:
            control.close_all()


if __name__ == "__main__":
    main()
