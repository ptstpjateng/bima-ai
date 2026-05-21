"""
Per-agent modules for BIMA's multi-agent architecture.

Each agent is a focused module with one job — see [[Decisions]] §18 for the
runtime model. Today: validator, officer_copilot. The officer_copilot module
also hosts the signature-assistant variant (`chat(mode="signature")`) — the
Head-of-DPMPTSP signing copilot for Vision req #13. Phase 3+ may add
submission_helper.
"""
