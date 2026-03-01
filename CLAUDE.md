# Agency Runner Protocol

Update `.agency/state/runner_status.json` at milestones:

| Status | When | Required Fields |
|--------|------|-----------------|
| `working` | Actively making progress | `summary` |
| `needs_input` | Waiting for user answer | `summary`, `questions[]` |
| `blocked` | Cannot proceed | `summary`, `blockers[]` |
| `ready_for_review` | Work complete | `summary`, `how_to_test` |

Schema:

```json
{
  "schema_version": "1.0",
  "status": "working",
  "updated_at": "2026-01-19T12:00:00Z",
  "summary": "Implementing user authentication",
  "questions": [],
  "blockers": [],
  "how_to_test": "",
  "risks": []
}
```

Before `ready_for_review`, update `.agency/report.md` with summary, decisions, testing instructions, and risks.
