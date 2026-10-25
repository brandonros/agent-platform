"""Append Claude Code hook events to the Command Center log.

The Command Center extension reads the log to link conversations to the
tickets tagged in their prompts ([ABC-123]) and to show which need attention.
Never blocks or fails the turn.
"""
import json
import os
import sys
import time

try:
    event = json.load(sys.stdin)
    record = {
        "ts": time.time(),
        "event": event.get("hook_event_name"),
        "session_id": event.get("session_id"),
        "cwd": event.get("cwd"),
        # sdk-* (claude -p, Jobs) conversations are hidden from the IDE panel.
        "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT"),
    }
    if "prompt" in event:
        record["prompt"] = event["prompt"][:2000]
    if "message" in event:
        record["message"] = event["message"][:500]
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    path = os.path.join(base, "command-center", "events.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
except Exception:
    pass
