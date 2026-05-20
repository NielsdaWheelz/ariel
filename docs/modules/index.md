# Module and Feature Docs

## Role

This directory contains docs owned by specific infrastructure modules or features.

## Docs

- [agent-loop.md](agent-loop.md): agent loop — async worker-run turns, the long adaptive loop, the scratch store, the research subagent, delivery
- [attachments.md](attachments.md): attachment content hard cutover
- [google-workspace-push-cutover.md](google-workspace-push-cutover.md): live
  Gmail + Calendar push hard cutover — Caddy reverse proxy, per-channel HMAC,
  Pub/Sub StreamingPull sidecar with exactly-once + dead-letter, daily watch
  renewal
- [google-workspace-reasoning-cutover.md](google-workspace-reasoning-cutover.md): Gmail,
  Calendar, commitment, due-date, and follow-up reasoning hard cutover
- [google-workspace-reasoning-completion-plan.md](google-workspace-reasoning-completion-plan.md):
  concrete Google Workspace reasoning completion plan
- [maps.md](maps.md): maps read vertical — directions and nearby-place capabilities
- [maps-expansion-cutover.md](maps-expansion-cutover.md): hard-cutover spec — multi-stop routing and alternative routes
- [memory.md](memory.md): memory subsystem — two-layer substrate (raw log + curated notes), agentic recall, and the rememberer
- [proactivity.md](proactivity.md): proactivity — the agent loop reached by non-human triggers, the scheduler, and provider ingestion
- [proactivity-cutover.md](proactivity-cutover.md): proactivity crystallization hard-cutover record
- [transport.md](transport.md): transport lifecycle ownership
