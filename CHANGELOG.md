# Changelog

## 0.10.0 - 2026-08-31

- Require UniRoboSim Core `>=0.10,<0.11` so the package installed from `main`
  resolves with the Core 0.10 release line.
- Verify the complete evidence, owned-session control, camera encoding, and MCP
  protocol suite against UniRoboSim Core 0.10.0.

## 0.9.0 - 2026-08-24

- Require UniRoboSim Core `>=0.9.1,<0.10` and align the package release identity with
  the Core 0.9 line.
- Encode camera PNG responses through Core 0.9.1's compact `ArrayValue.to_bytes()`
  path so packed RGB frames are not expanded into Python integer tuples.
- Verify the complete fake-backend control, evidence-query, real MCP client, lint,
  type-check, wheel, fresh-target metadata, and fixed-candidate coexistence gates,
  including all public tools through a visible Isaac Lab 0.9.3 stdio session.
- Document coexistence with the FastSim 0.1.0a1 and UniRoboSim Isaac Lab 0.9.3
  candidate wheels without making either package a runtime dependency.

## 0.7.0 - 2026-08-19

- Upgrade the bounded evidence service to Core `>=0.7.0,<0.8`.
- Add explicitly enabled, owned-session simulation control with leases, idempotency, limits, and JSONL audit records.
- Add scene discovery and typed reads for rigid bodies, articulations, deformables, particle fluids, and cameras.
- Add MCP image responses encoded directly from backend RGB camera buffers, with optional hashed evidence output.
- Retain evidence-only operation as the default deployment profile.
- Support both MCP 2.x and the MCP 1.10.1 compatibility runtime required by the verified
  Isaac Lab 3.0 dependency stack.
