#!/usr/bin/env python3
"""Dispatch one Claude Code turn as a Kubernetes Job in the shared workspace.

The Job spec comes from the claude-job-template ConfigMap (values/claude-job.yaml.gotmpl);
this script only names it and sets the prompt, session, and labels. Pass --session
to continue an existing session; omit it to start one. The Job name is derived
from --turn-id when given, so dispatching the same turn twice reuses its Job.

Usage: claude-job.py "PROMPT" [--session UUID] [--turn-id ID] [--started-by WHO] [--wait]
"""
import argparse
import getpass
import hashlib
import json
import re
import subprocess
import sys
import uuid


def kubectl(*args, input=None):
    return subprocess.run(["kubectl", "-n", "agent-platform", *args], input=input,
                          capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description="Run a Claude Code turn as a Kubernetes Job.")
    parser.add_argument("prompt")
    parser.add_argument("--session", type=uuid.UUID, help="existing session UUID to continue")
    parser.add_argument("--turn-id", help="stable ID for this turn; dispatching it again reuses its Job")
    parser.add_argument("--started-by", default=getpass.getuser(), help="who is starting this turn")
    parser.add_argument("--wait", action="store_true", help="wait and print the result")
    args = parser.parse_args()

    session_id = str(args.session or uuid.uuid4())
    name = "claude-" + hashlib.sha256((args.turn_id or str(uuid.uuid4())).encode()).hexdigest()[:16]

    template = kubectl("get", "configmap", "claude-job-template", "-o", "jsonpath={.data.job\\.json}").stdout
    if not template:
        sys.exit("claude-job-template not found; run: just apply")
    job = json.loads(template)
    job["metadata"]["name"] = name
    # Label values allow [A-Za-z0-9._-], at most 63 characters, alphanumeric at both ends.
    started_by = re.sub(r"[^A-Za-z0-9._-]", "_", args.started_by)[:63].strip("._-") or "unknown"
    for metadata in (job["metadata"], job["spec"]["template"]["metadata"]):
        metadata["labels"].update({"claude-session": session_id, "claude-started-by": started_by})
    values = {"PROMPT": args.prompt, "SESSION_ID": session_id}
    for variable in job["spec"]["template"]["spec"]["containers"][0]["env"]:
        if variable["name"] in values:
            variable["value"] = values[variable["name"]]

    created = kubectl("create", "-f", "-", input=json.dumps(job))
    if created.returncode and "AlreadyExists" not in created.stderr:
        sys.exit(created.stderr.strip())
    if created.returncode:
        session_id = kubectl("get", "job", name, "-o", "jsonpath={.metadata.labels.claude-session}").stdout
    print(json.dumps({"job": name, "session_id": session_id, "reused": created.returncode != 0}), flush=True)

    if args.wait:
        # Follows the log until the container exits; the result is its last line.
        logs = kubectl("logs", "-f", f"job/{name}", "--pod-running-timeout=10m").stdout.strip()
        try:
            print(json.dumps(json.loads(logs.splitlines()[-1]), indent=2))
        except (IndexError, json.JSONDecodeError):
            print(json.dumps({"is_error": True, "result": logs[-2000:]}, indent=2))


if __name__ == "__main__":
    main()
