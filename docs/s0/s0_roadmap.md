# Slice 0: Private Walking Skeleton — PR Roadmap

### PR-01: Durable Single-Session Chat Skeleton
- **goal**: stand up a minimal Ariel backend + phone-friendly web chat that handles one full model-backed turn and persists the full audit chain in Postgres.
- **builds on**: none (current repo state is docs-only).
- **status**: implemented (2026-03-01)
- **acceptance**:
  - user can send a message in the chat surface and receive a model-backed response for that same turn.
  - Ariel maintains one active session and appends each new turn into that session.
  - each turn writes an ordered event chain including turn start, model start/end, assistant response, and terminal completed/failed status.
  - timeline UI renders turn history from the stored event chain and shows provider/model identity, duration, usage metadata when available, and failure reason when present.
- **non-goals**: tailnet/private ingress hardening and restart-proof validation.

### PR-02: Private Tailnet Access + Restart Durability Hardening (planned after PR-01 merges)
- **goal**: make the walking skeleton usable from phone over private routing only and prove history continuity across restarts.
- **builds on**: PR-01.
- **acceptance**:
  - app service binds localhost only; phone reaches Ariel through `tailscale serve` HTTPS on the node's private tailnet DNS name.
  - no public ingress is enabled for Ariel (no funnel/public exposure), and this is verifiable in deployment state.
  - tailnet policy restricts Ariel access to explicitly allowed user/device identities only.
  - after service restart, prior conversation history and timeline remain visible, and new turns append to the same active session.
  - model failures still produce auditable model/timeline events with explicit terminal failure state and reason.
  - deployment/run workflow documents a repeatable private setup for a self-hosted machine.
- **non-goals**: capability execution, approval workflows, agency/calendar integrations, durable memory retrieval, multi-user/public hosting.
