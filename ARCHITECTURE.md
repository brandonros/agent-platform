# Architecture

agent-platform is a single-tenant Kubernetes lab providing Claude Code turns as
Jobs and the real Claude Code extension through code-server. See [README.md](README.md)
for setup, commands, limitations, and recovery.

## Session storage

The existing code-server release owns a 10 GiB RWO PVC. Its legacy workspace is
preserved. New work is stored in `sessions/<session-uuid>/workspace` and
`sessions/<session-uuid>/claude`. Each session may contain any number of repos.
Directories survive Job and IDE Pod deletion; no automatic archival is implemented.

Every turn mounts the session's two subpaths at `/home/coder/workspace` and
`/home/coder/.claude`. A setup container creates them as uid 1000. The main
container never mounts the PVC root. Stable internal paths preserve Claude's
encoded project path `-home-coder-workspace` across Jobs and IDEs.

All workloads still use required affinity to the legacy code-server Deployment
for RWO locality. Per-session directories solve checkout collisions, not the
single-node constraint or user credential isolation.

## Automated turns

`claude-job.py` reads the Helm-installed `claude-job-template` ConfigMap, adds
session mounts and initialization, acquires the session Lease, and creates a
Job. `--turn-id` determines its name. An existing Job is reused; a conflicting
explicit session UUID is rejected. Deduplication ends when the Job is deleted.

The Job resumes the conversation if its JSONL exists, otherwise starts it with
`--session-id`. It uses `bypassPermissions`, JSON output, and
`CLAUDE_CODE_ENTRYPOINT=claude-vscode`. Deadline: one hour; retries: zero;
completed Job TTL: 24 hours. Results remain in pod logs, not a durable run store.

## Interactive handoff

`claude-session.py open <uuid>` acquires the same session Lease and creates a
session-specific code-server Pod using the `ide-image` from the ConfigMap. It
uses the same mounts and init container as the Jobs. The CLI port-forwards to
localhost:8080; users select the conversation in the extension's Session Manager.

When oauth2-proxy is deployed, its container configuration is copied into a
sidecar, with its upstream changed to localhost:8080. The forward targets that
proxy on 4180. No session Service/ingress is created. Entra secrets without a
proxy Deployment cause opening to fail rather than silently bypassing sign-in.
Without Entra configured, the local forward goes directly to the IDE.

An open IDE owns the entire session until explicitly closed. Disconnecting the
browser or port-forward does not release it. Closing uses a conditional Lease
update to select one closer, deletes the Pod and waits, then conditionally
releases ownership. Failed closes remain reserved for operator recovery.

## Lock protocol

`session_runtime.py` implements create-or-resourceVersion-replace acquisition.
Each acquisition has a unique holder token and annotations naming its Job/Pod.
The previous holder is read directly and must be terminal before takeover.
An additional active-Job check catches older dispatchers where possible; it is
not a substitute for every caller participating in the protocol.

Missing holder workloads are not reclaimed on a timer. A slow creator could
still submit after the timer. Uncertain API failures retain the reservation;
operators must quiesce callers before recovery. Job reservations get an owner
reference via conditional replacement for garbage collection with the Job.
Successors remove that ownership on acquisition. Interactive reservations remain
until explicit close. Cleanup uses token comparison and resourceVersion, never
an unconditional Lease deletion.

This protocol does not promise exactly-once external side effects and cannot
protect against direct kubectl writers, old scripts, or arbitrary users acting
outside it. Old shared-workspace sessions are not automatically migrated.

## Images and configuration

`claude-base` contains the CLI, git, Python, managed instructions/settings in
`/etc/claude-code`, and the org plugin in `/opt/claude-org`. `code-server` derives
from it and installs the editor and extension. Tags are in `image-tags.yaml`;
`just build-image` builds in Lima and imports into k3s. Org config changes require
both images to be rebuilt. The CLI and extension installers are not version-pinned.

Session Pods and Jobs disable service-account token mounting. They use the same
Claude OAuth secret and uid 1000. This remains a trusted, single-tenant lab:
per-session working state is not per-user authorization. No network policy is
provided. Prompts are visible in Job environment specifications to Job readers.
