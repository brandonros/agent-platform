# Workflow clients

Runnable examples, not tests. Only Python 3's standard library is required.
The shared client handles HTTP; each workflow example supplies its own type,
queue, inputs, and signal contract.

Export WORKFLOW_API_TOKEN (the lab's existing API key) and optionally
WORKFLOW_API_URL. These scripts do not automatically read .env. Run from the
repository root:

```bash
python3 examples/workflow-clients/meeting_notes.py "Sam owns release notes, due Friday." --wait
python3 examples/workflow-clients/agent_capabilities.py --wait
python3 examples/workflow-clients/draft_review.py start "A staff writing pilot"
python3 examples/workflow-clients/client.py show WORKFLOW_ID
python3 examples/workflow-clients/draft_review.py revise WORKFLOW_ID "Shorten it"
python3 examples/workflow-clients/draft_review.py approve WORKFLOW_ID
python3 examples/workflow-clients/client.py wait WORKFLOW_ID
```

Starts print an ID immediately. Supply --key with the same value to safely retry
a start with identical input. A --wait option polls for up to five minutes;
timing out or closing the client does not cancel the workflow. Draft review
waits for a human decision, so it does not automatically wait on start.

The shared client also works with any workflow or signal:

```bash
python3 examples/workflow-clients/client.py start MeetingNotes meeting-notes '[{"notes":"Sam owns release notes, due Friday."}]'
python3 examples/workflow-clients/client.py signal WORKFLOW_ID submit_review '["approve"]'
```

No state or reports are written locally. A signal response confirms acceptance,
not completed processing; poll afterward to observe the outcome.
