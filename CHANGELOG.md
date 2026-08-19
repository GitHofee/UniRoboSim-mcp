# Changelog

## 0.7.0 - 2026-08-19

- Upgrade the bounded evidence service to Core `>=0.7.0,<0.8`.
- Add explicitly enabled, owned-session simulation control with leases, idempotency, limits, and JSONL audit records.
- Add scene discovery and typed reads for rigid bodies, articulations, deformables, particle fluids, and cameras.
- Add MCP image responses encoded directly from backend RGB camera buffers, with optional hashed evidence output.
- Retain evidence-only operation as the default deployment profile.
- Support both MCP 2.x and the MCP 1.10.1 compatibility runtime required by the verified
  Isaac Lab 3.0 dependency stack.
