# UniRoboSim MCP

[English](README.md) | [简体中文](README.zh-CN.md)

`unirobosim-mcp` exposes UniRoboSim evidence, simulation state, backend camera images, and explicitly enabled simulation control to MCP-compatible clients. The server has two deployment profiles:

- **Evidence profile (default):** bounded, read-only access to an operator-selected evidence root.
- **Control profile (explicit):** Evidence tools plus Read and Control tools for simulator sessions created and owned by this server.

The server does not attach to sessions created by other applications.

## Installation

Python `>=3.11,<3.13` is supported. Install Core, this package, and the adapter required by the selected backend in the same environment.

```bash
conda create -n unirobosim-mcp python=3.12 pip -y
conda activate unirobosim-mcp

git clone https://github.com/GitHofee/UniRoboSim.git
git clone https://github.com/GitHofee/UniRoboSim-mcp.git
git clone https://github.com/GitHofee/UniRoboSim-mujoco.git  # example backend

python -m pip install ./UniRoboSim ./UniRoboSim-mcp ./UniRoboSim-mujoco
```

General deployments use the current MCP 2.x runtime. Isaac Lab 3.0 environments retain
their verified Pydantic and Uvicorn pins, so install the compatibility extra there:

```bash
python -m pip install './UniRoboSim-mcp[isaaclab]'
```

The extra selects MCP `1.10.1`; it exposes the same UniRoboSim tool catalog and was
verified through the real stdio protocol with Isaac Sim 6.0.1.

## Evidence profile

```bash
unirobosim-mcp --root /absolute/path/to/approved/evidence
```

`UNIROBOSIM_EVIDENCE_ROOT` may be used instead of `--root`:

```bash
export UNIROBOSIM_EVIDENCE_ROOT=/absolute/path/to/approved/evidence
unirobosim-mcp
```

| Tool | Contract |
| --- | --- |
| `evidence_server_info` | Return the active root, hard query limits, and control status. |
| `list_debug_evidence` | List allowlisted evidence with a bounded POSIX glob. |
| `read_debug_evidence` | Read one bounded UTF-8 or JSON artifact. |
| `summarize_debug_trace` | Validate a closed trace and return its compact manifest. |
| `query_debug_events` | Query publish, clear, and reset events without full geometry. |
| `query_debug_reports` | Query accepted, filtered, and dropped publish decisions. |
| `query_debug_primitives` | Reconstruct selected active debug primitives at a sequence. |

Absolute paths, traversal, escaping symlinks, unapproved extensions, oversized files, excessive scans, and excessive result counts are rejected.

## Control profile

Control must be enabled explicitly. Local asset files are denied unless their parent tree is allowlisted with `--asset-root`.

```bash
unirobosim-mcp \
  --root /absolute/path/to/approved/evidence \
  --enable-control \
  --asset-root /absolute/path/to/approved/assets \
  --max-sessions 2 \
  --lease-timeout-seconds 300
```

### Read API

Read tools require a session ID but not the write lease.

| Tool | Contract |
| --- | --- |
| `simulation_list_backends` | Discover and probe installed backend entry points. |
| `simulation_list_sessions` | List sessions owned by this server; lease values are never returned. |
| `simulation_scene_snapshot` | Return the portable scene graph for entity and camera discovery. |
| `simulation_get_entity` | Read typed state for a rigid body, articulation, deformable, particle fluid, or camera. |
| `simulation_capture_camera` | Return an MCP image containing PNG data encoded from the backend RGB camera buffer. |

`simulation_get_entity` reports the canonical path, entity kind, original MCP configuration, simulation tick, array shapes and dtypes, and type-specific data. `include_values=true` includes bounded values; `include_contact=true` adds rigid-body contact state.

`simulation_capture_camera` is not a desktop or browser screenshot. It calls the selected backend through `Camera.read("rgb")`, validates the canonical `[environment,height,width,3]` uint8 buffer, and encodes that buffer as PNG. `save_to_evidence=true` also writes the image under `<root>/screenshots/` and returns its SHA-256 digest and dimensions.

### Control API

All mutations require the opaque `lease_id` returned by `simulation_create` and a unique `command_id`.

| Tool | Contract |
| --- | --- |
| `simulation_control_info` | Return ownership policy, allowlisted roots, and hard resource limits. |
| `simulation_create` | Create an owned EasyAPI session for an explicit backend. |
| `simulation_configure_entity` | Add a box, rigid asset, articulation, camera, deformable, or particle fluid before start. |
| `simulation_start` | Compile the scene and return its backend build fingerprint. |
| `simulation_renew_lease` | Extend the write lease without changing its value. |
| `simulation_step` | Advance the simulation by a bounded number of steps. |
| `simulation_reset` | Reset all or selected environments. |
| `simulation_command` | Apply articulation, rigid-wrench, deformable, fluid, scene, or debug-clear commands. |
| `simulation_close` | Close the owned session and release backend resources. |

Repeated use of a `command_id` with identical input returns the cached result with `idempotent_replay=true`. Reusing that identifier with different input is rejected. Expired sessions are closed automatically. Every applied or rejected mutation is written to `mcp-control-audit.jsonl`; lease values are excluded from the audit record.

## Agent operating rule

An agent using the Control profile must follow this sequence:

1. Call `simulation_list_backends` and select an available backend explicitly.
2. Call `simulation_create`; retain the returned lease only for write operations.
3. Add all entities with unique command identifiers, then call `simulation_start`.
4. Use `simulation_scene_snapshot` to discover canonical entity and camera paths.
5. Use `simulation_get_entity` for targeted state and `simulation_capture_camera` for visual verification.
6. Reuse a command identifier only to retry the identical write request.
7. Call `simulation_close` for every created session, including failed workflows.

The agent must not infer backend support from tool availability. Unsupported simulator capabilities are reported by capability negotiation or by the selected adapter.

## Loopback HTTP

```bash
unirobosim-mcp \
  --root /absolute/path/to/approved/evidence \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8766
```

Unauthenticated HTTP is restricted to `127.0.0.1`, `localhost`, or `::1`. Remote deployment requires an authenticated and authorized gateway. Control mode must not be exposed directly on an untrusted network.

## Programmatic embedding

```python
from pathlib import Path

from unirobosim_mcp import ControlLimits, EvidenceLimits, SimulationControl, create_server

root = Path("/approved/evidence")
control = SimulationControl(
    root,
    asset_roots=(Path("/approved/assets"),),
    limits=ControlLimits(max_sessions=1, lease_timeout_seconds=120),
)
server = create_server(
    root,
    limits=EvidenceLimits(max_results=50, max_query_items=100),
    control=control,
)
server.run(transport="stdio")
```

## Verification

```bash
python -m pip install -e '.[dev]'
ruff format --check src tests
ruff check src tests
mypy src
coverage run -m pytest
coverage report
```

Release acceptance calls every published MCP tool through a real in-process MCP client. Additional contract tests cover all supported entity types and command families, leases, idempotency, expiration, allowlisted assets, resource limits, audit records, PNG encoding, and saved screenshot evidence. Native acceptance is executed separately for each installed simulator adapter; a feature is not reported as passed for a backend unless that native run succeeds.

Core contracts and adapter installation are documented in [UniRoboSim Core](https://github.com/GitHofee/UniRoboSim.git).
