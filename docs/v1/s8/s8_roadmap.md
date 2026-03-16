# Slice 8: Quick Capture Surface — PR Roadmap

current state: pr-01 and pr-02 are merged, but Slice 8 is not complete yet.

remaining gaps in merged state:
- capture-created turns appear in the normal turn/timeline surfaces, but those surfaced contracts do not link back to the durable `cpt_` record, so capture auditability is still only available in the immediate `POST /v1/captures` response or the database.
- terminal capture records are durable in storage but not user-inspectable after creation, which leaves ingest failures effectively ephemeral at the API surface.
- only `shared_content` capture ingress is runtime-tainted today; `text` and `url` captures can still drive allowlisted write-reversible capabilities without capture-origin taint escalation.

### PR-03: Capture Safety Parity for Text + URL
- **goal**: extend capture-origin safety semantics to all capture kinds so observe-first capture input cannot auto-run allowlisted side effects while still using Ariel's normal action and approval lifecycle.
- **builds on**: PR-02.
- **acceptance**:
  - `text`, `url`, and `shared_content` captures all mark shared source material as untrusted ingress provenance for runtime policy evaluation instead of relying on prompt wording alone.
  - bare captures cannot inline write-reversible or external-send actions through allowlisted capabilities; capture-origin side-effect proposals escalate or deny under the existing taint rules.
  - capture-origin proposals that require approval still surface the same proposal, approval, and execution lifecycle as normal chat turns and can be completed through the existing approvals flow.
  - regression coverage blocks release on the previously-open loophole where `text` or `url` captures could auto-run allow-inline write-reversible capabilities.
- **non-goals**: changing chat-turn policy outside capture ingress; adding chat/mobile capture UX; multimodal or batched capture.

### PR-04: Capture Audit Surface Completion
- **goal**: make durable capture records actually inspectable at the surfaced API and link capture identity into unified turn history so Slice 8 meets its auditability requirements without a second conversation-history model.
- **builds on**: PR-03.
- **acceptance**:
  - turn and timeline surfaces for capture-created turns expose stable capture linkage for auditability, including capture identity and kind.
  - Ariel adds a read-only capture inspection path so successful captures and ingest failures remain inspectable after the initial `POST /v1/captures` response.
  - surfaced capture inspection distinguishes ingest rejection from post-turn failure while preserving stable retry and recovery guidance.
  - regression coverage proves capture linkage in surfaced history and durable lookup of terminal capture outcomes without database access.
- **non-goals**: capture list/search UX; background/offline mobile sync; binary, vision, or audio capture; new conversation-history surfaces separate from turns.
