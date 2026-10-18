#!/usr/bin/env python3
"""Dispatch one Claude Code turn as a Kubernetes Job in the shared workspace.

The Job spec comes from the claude-job-template ConfigMap (values/claude-job.yaml.gotmpl);
this script only names it and sets the prompt, session, and labels. Pass --session
to continue an existing session; omit it to start one. The Job name is derived
from --turn-id when given, so dispatching the same turn twice reuses its Job.

One turn per session at a time: the script refuses while a Job for the session
is still running, and takes the session's Lease (claude-session-<id>) so two
dispatchers can't both start one. The Lease is owned by the Job, so Kubernetes
deletes it with the Job.

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
from datetime import datetime, timezone

# A fresh Lease is honored this long before its Job shows up.
STARTING_GRACE_SECONDS = 60


def kubectl(*args, input=None):
    return subprocess.run(["kubectl", "-n", "agent-platform", *args], input=input,
                          capture_output=True, text=True)


def active_jobs(session_id):
    jobs = json.loads(kubectl("get", "jobs", "-l", f"claude-session={session_id}", "-o", "json").stdout)["items"]
    done = lambda job: any(c["type"] in ("Complete", "Failed") and c["status"] == "True"
                           for c in job.get("status", {}).get("conditions", []))
    return [job["metadata"]["name"] for job in jobs if not done(job)]


def lock(session_id, holder):
    """Take the session's Lease for holder, or exit if another turn has the session."""
    busy = active_jobs(session_id)
    if busy:
        sys.exit(f"Session {session_id} is busy: {', '.join(busy)} is still running")
    now = datetime.now(timezone.utc)
    lease = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
             "metadata": {"name": f"claude-session-{session_id}", "labels": {"claude-session": session_id}},
             "spec": {"holderIdentity": holder, "acquireTime": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}}
    if kubectl("create", "-f", "-", input=json.dumps(lease)).returncode == 0:
        return
    # The Lease exists but no Job is running: take it over if its Job finished, or if
    # its Job never appeared within the grace period (a dispatcher died mid-start).
    # replace with the read resourceVersion fails if another dispatcher took it first.
    current = json.loads(kubectl("get", "lease", lease["metadata"]["name"], "-o", "json").stdout or "null")
    if current:
        acquired = datetime.fromisoformat(current["spec"]["acquireTime"].replace("Z", "+00:00"))
        finished = kubectl("get", "job", current["spec"]["holderIdentity"]).returncode == 0
        if finished or (now - acquired).total_seconds() >= STARTING_GRACE_SECONDS:
            lease["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
            if kubectl("replace", "-f", "-", input=json.dumps(lease)).returncode == 0:
                return
    sys.exit(f"Session {session_id} is busy: another turn is starting")


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

    existing = kubectl("get", "job", name, "-o", "jsonpath={.metadata.labels.claude-session}")
    reused = existing.returncode == 0
    if reused:
        session_id = existing.stdout
    else:
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

        lock(session_id, name)
        created = kubectl("create", "-f", "-", "-o", "jsonpath={.metadata.uid}", input=json.dumps(job))
        if created.returncode:
            kubectl("delete", "lease", f"claude-session-{session_id}")
            sys.exit(created.stderr.strip())
        owner = {"apiVersion": "batch/v1", "kind": "Job", "name": name, "uid": created.stdout}
        kubectl("patch", "lease", f"claude-session-{session_id}", "--type", "merge",
                "-p", json.dumps({"metadata": {"ownerReferences": [owner]}}))
    print(json.dumps({"job": name, "session_id": session_id, "reused": reused}), flush=True)

    if args.wait:
        # Follows the log until the container exits; the result is its last line.
        logs = kubectl("logs", "-f", f"job/{name}", "--pod-running-timeout=10m").stdout.strip()
        try:
            print(json.dumps(json.loads(logs.splitlines()[-1]), indent=2))
        except (IndexError, json.JSONDecodeError):
            print(json.dumps({"is_error": True, "result": logs[-2000:]}, indent=2))


if __name__ == "__main__":
    main()
