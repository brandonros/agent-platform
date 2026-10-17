# Temporal workers

```text
worker.py                 Starts the selected worker and registers its functions
workflows/                One file per use case, including its specific helpers
  draft_review.py         Draft → critique → revise → human approval
  meeting_notes.py        Extract actions → clarify or summarize
  capabilities.py         Agent/skill/MCP demonstration
framework/                Shared code reused by all use cases
  webui.py                Login, saved chats, send/wait for replies, heartbeats
  models.py               Shared Turn input
```

A Temporal **workflow** decides what happens next and remembers its progress.
An **activity** performs external work, such as calling WebUI. Temporal can retry
that work without starting the entire workflow again. These are function roles,
not a requirement for separate directories.

Each workflow owns its prompts, schemas, validation, and branching.
All workflows use the same two activity types: create_chat and claude_turn.
Keep reusable WebUI communication in the framework. `worker.py` registers both
workflow and activity functions with Temporal.

Each deployment chooses WORKER_KIND (`draft-review` or `meeting-notes`) and
polls its own queue. Both use `values/temporal-workers.yaml.gotmpl`.
