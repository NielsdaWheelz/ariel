# AI-First SOTA Gap Cutover

## Scope

This document records the active AI-first cutover gaps that must stay explicit in
docs and tests. It is not a compatibility promise for removed runtime surfaces.

### Failure Code And Status Vocabulary

AI judgment failures use only these typed failure codes:

- E_AI_JUDGMENT_REQUIRED
- E_AI_JUDGMENT_CREDENTIALS
- E_AI_JUDGMENT_TIMEOUT
- E_AI_JUDGMENT_INVALID_JSON
- E_AI_JUDGMENT_SCHEMA
- E_AI_JUDGMENT_VALIDATION
- E_AI_JUDGMENT_BUDGET

### Ambient Source Coverage

Unconfigured ambient source families are absent until a real connector or
provider path exists for them. That absence is intentional for CI, location,
local activity, repository, and incident sources.
