"""MCP access to UniRoboSim evidence and explicitly enabled simulation control."""

__version__ = "0.9.0"

from .control import ControlAccessError, ControlLimits, Screenshot, SimulationControl
from .query import query_events, query_primitives, query_reports, summarize_trace
from .server import create_server
from .store import EvidenceLimits, EvidenceStore

__all__ = [
    "ControlAccessError",
    "ControlLimits",
    "EvidenceLimits",
    "EvidenceStore",
    "Screenshot",
    "SimulationControl",
    "__version__",
    "create_server",
    "query_events",
    "query_primitives",
    "query_reports",
    "summarize_trace",
]
