#!/usr/bin/env python3
"""Dispatch one Claude Code turn as a Kubernetes Job in the shared workspace.

The Job spec comes from the claude-job-template ConfigMap (values/claude-job.yaml.gotmpl);
this script only names it and sets the prompt and session. Pass --session to
continue an existing session; omit it to start one. The Job name is derived
from --turn-id when given, so dispatching the same turn twice reuses its Job.

Every Job records who started it: the exact value in the claude.started-by
annotation, and a label-safe copy in the claude-started-by label for filtering.

Usage: claude-job.py "PROMPT" [--session UUID] [--turn-id ID] [--started-by WHO] [--wait]
"""
import argparse
import getpass
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid

NAMESPACE = "agent-platform"


def default_caller():
    email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True).stdout.strip()
    return email or getpass.getuser()


def label_value(value):
    # Labels allow [A-Za-z0-9._-], at most 63 characters, alphanumeric at both ends.
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:63].strip("._-") or "unknown"


def kubectl(*args, input=None):
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], input=input,
                          capture_output=True, text=True)


def build(name, session_id, prompt, started_by):
    template = kubectl("get", "configmap", "claude-job-template", "-o", "jsonpath={.data.job\\.json}")
    if template.returncode or not template.stdout:
        sys.exit("claude-job-template not found; run: helmfile -l name=claude-job sync")
    job = json.loads(template.stdout)
    job["metadata"]["name"] = name
    for metadata in (job["metadata"], job["spec"]["template"]["metadata"]):
        metadata["labels"]["claude-session"] = session_id
        metadata["labels"]["claude-started-by"] = label_value(started_by)
        metadata.setdefault("annotations", {})["claude.started-by"] = started_by
    for variable in job["spec"]["template"]["spec"]["containers"][0]["env"]:
        if variable["name"] == "PROMPT":
            variable["value"] = prompt
        elif variable["name"] == "SESSION_ID":
            variable["value"] = session_id
    return job


def wait(name):
    while True:
        status = json.loads(kubectl("get", "job", name, "-o", "json").stdout).get("status", {})
        if status.get("succeeded") or status.get("failed"):
            break
        time.sleep(2)
    logs = kubectl("logs", f"job/{name}").stdout
    try:
        return json.loads(logs.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"is_error": True, "result": logs[-2000:]}


def main():
    parser = argparse.ArgumentParser(description="Run a Claude Code turn as a Kubernetes Job.")
    parser.add_argument("prompt")
    parser.add_argument("--session", help="existing session UUID to continue")
    parser.add_argument("--turn-id", help="stable ID for this turn; dispatching it again reuses its Job")
    parser.add_argument("--started-by", default=default_caller(),
                        help="who is starting this turn (default: git user.email, else login name)")
    parser.add_argument("--wait", action="store_true", help="wait and print the result")
    args = parser.parse_args()

    session_id = str(uuid.UUID(args.session)) if args.session else str(uuid.uuid4())
    name = "claude-" + hashlib.sha256((args.turn_id or str(uuid.uuid4())).encode()).hexdigest()[:16]
    created = kubectl("create", "-f", "-", input=json.dumps(build(name, session_id, args.prompt, args.started_by)))
    if created.returncode and "AlreadyExists" not in created.stderr:
        sys.exit(created.stderr.strip())
    print(json.dumps({"job": name, "session_id": session_id, "started_by": args.started_by,
                      "reused": created.returncode != 0}), flush=True)
    if args.wait:
        print(json.dumps(wait(name), indent=2))


if __name__ == "__main__":
    main()
