# agent-platform

Organizational Claude Code in a local Kubernetes lab. People use the real Claude
Code (VS Code extension) in a browser through code-server. Automated work runs
as Kubernetes Jobs in persistent per-session workspaces. A session-specific
browser IDE opens the same files and conversation history. Org agents, skills, MCP servers, and instructions are baked
into the image as managed configuration.

## Components

| App | Helm values | Purpose |
|---|---|---|
| `apps/claude-base/` | (image only) | `claude-base` image: Claude Code CLI, git, python3, org config. Jobs run it |
| `apps/code-server/` | `values/code-server.yaml.gotmpl` | `code-server` image, built `FROM claude-base`: code-server, Claude Code, and the Command Center extension. One release per user (`code-server-<id>`) |
| `users.yaml` | (read by values and scripts) | Users: id, name, and email for git commits and Jira |
| `apps/mcp-test-server/` | `values/mcp-test-server.yaml.gotmpl` | Cluster MCP fixture: `echo` tool and `echo_test` prompt |
| (Job template) | `values/claude-job.yaml.gotmpl` | `claude-job-template` ConfigMap: the Job spec for one Claude Code turn (`claude-base` image) |
| `scripts/claude-job.py` | (none) | Dispatches a turn: fills the template's name, prompt, session, and user |
| (upstream) | `values/oauth2-proxy.yaml.gotmpl` | Entra ID sign-in in front of code-server |

## Deploy

Requires Lima 2+, Helmfile, Helm, just, kubectl, Python 3, and OpenSSL. Copy
`.env.example` to `.env` and set `CLAUDE_CODE_OAUTH_TOKEN`, then:

```bash
just bootstrap            # VM "agent-platform", namespace "agent-platform"
just code-server brandon  # http://localhost:8080 (a user id from users.yaml)
```

Each user in `users.yaml` gets a `code-server-<id>` release with its own 10 GiB
PVC, commit identity, and `${userEmail}` for Jira queries. To add a user, add
them to `users.yaml` and a release to `helmfile.yaml`, then `just apply`; open
theirs with `just code-server <id> 8081`. Jira and GitHub credentials are shared
Secrets from `.env` (`JIRA_*`, `GH_TOKEN`).

code-server runs with `--auth none` and has no ingress. Sign-in is optional
Microsoft Entra ID through oauth2-proxy. Without it, `just code-server` forwards
straight to code-server, reachable only from your machine.

To add Entra ID sign-in:

1. Entra admin center → App registrations → New registration. Single tenant.
   Redirect URI: platform **Web**, `http://localhost:8080/oauth2/callback`.
2. Certificates & secrets → New client secret.
3. Set `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID` (application ID), and
   `ENTRA_CLIENT_SECRET` in `.env`, then `just secrets && just apply`.

`just apply` deploys oauth2-proxy only when its Secret exists, and `just code-server`
then forwards through it. Any account in the tenant may sign in; restrict it with
`OAUTH2_PROXY_ALLOWED_GROUPS` (group object IDs) in `values/oauth2-proxy.yaml.gotmpl`.
One proxy fronts one IDE: the first user in `users.yaml`.

Use `localhost`: browsers block service workers and webviews (including the
Claude Code panel) on plain-HTTP hosts, and the Entra redirect URI is registered
for `localhost`. `kubectl port-forward` exits when a connection breaks, so the
recipe restarts it.

Two images, tagged in `image-tags.yaml`. `code-server` is built `FROM
claude-base:<claude-base tag>`, so after changing org config, agents, skills, or
the CLI, rebuild both:

```bash
# bump both tags in image-tags.yaml, then:
just build-image claude-base
just build-image code-server
helmfile -l name=code-server-brandon -l name=claude-job sync
```

For editor-only changes (`apps/code-server/Dockerfile`), bump and rebuild only
`code-server`.

The VS Code extension bundles its own Claude Code, so IDE sessions and Jobs can
run slightly different Claude Code versions; they share the same session files.

## Session workspaces and turns

Apply the updated template first (`just apply`); no image rebuild is needed for
this change. Start a task:

```bash
just claude-job "Clone the repos needed for this task and investigate ..." --wait
# Output includes session_id. Use that UUID below:
just claude-job "Continue the investigation" --session <session-uuid> --wait
just session-open <session-uuid>
# Open http://localhost:8080 and select the conversation in Claude Session Manager.
# When finished (from another terminal, or after Ctrl-C):
just session-close <session-uuid>
just claude-job "Continue after my edits" --session <session-uuid> --wait
```

A new session starts empty. Claude can clone any number of repositories into it;
there is no single-repository requirement. Files, uncommitted changes, and Claude
history persist between turns. Sessions belong to a user (`--user`, required
when `users.yaml` lists several) and live on that user's PVC:

```text
sessions/<uuid>/workspace/   # all repositories and working files for this task
sessions/<uuid>/claude/      # private-to-this-session Claude state and history
```

Jobs and the session IDE mount these at `/home/coder/workspace` and
`/home/coder/.claude`. An init container creates missing directories as uid 1000.
Only setup mounts the PVC root; the main container mounts its session subpaths.
This prevents accidental checkout collisions, not a hostile-user security boundary.

`session-open` creates one code-server Pod for the session. It reserves the whole
session, even when idle or when port-forwarding stops. `session-close` deletes the
Pod, waits for termination, and releases ownership. It never deletes workspace
files. Stop the existing `just code-server` forward before opening a session;
both use localhost:8080. When Entra is deployed, the session Pod includes a copy
of the oauth2-proxy container configuration, forwarding to its local IDE. Without
Entra, access is through a localhost-only kubectl port-forward. No Service or
ingress is created for session IDEs. A failed readiness wait leaves the reservation
in place: retry opening or close the IDE.

Automated sessions appear in their own session IDE, not in the user's
`just code-server` IDE. Follow-up automation must use the original UUID returned
by the dispatcher, even if you open additional conversations inside that IDE.

### Jobs, ownership, and failure recovery

- `--turn-id` deduplicates by Job name **only while that Job exists**. Jobs expire
  24 hours after completion; repeating an ID after deletion may execute it again.
  It is not an exactly-once guarantee for external actions.
- Jobs have a one-hour deadline and no retries. JSON output remains in pod logs;
  durable business results/run records are not implemented.
- A Lease serializes cooperating dispatchers and reserves interactive ownership.
  Completed Jobs permit the next turn; running Jobs and open IDEs block it.
  Conditional updates prevent an old caller from releasing a successor's lock.
- If creation fails with an uncertain outcome, the Lease stays reserved. We do
  not steal a lock after a timeout: a delayed caller could still create its Job.
- Job-owned Leases are garbage-collected with their Jobs. A crash before ownership
  is attached, an absent holder, or a failed IDE close may need operator recovery.

For recovery, inspect the Lease and the Job/Pod named in its
`session-resource-kind` / `session-resource-name` annotations. Legacy Leases use
`holderIdentity` as the Job name. Stop all dispatchers/open/close commands for that
session, and ensure its workload has terminated before deleting its Lease:

```bash
kubectl -n agent-platform get lease claude-session-<uuid> -o yaml
# Only after quiescing callers and terminating the holder:
kubectl -n agent-platform delete lease claude-session-<uuid>
```

Do not delete a Lease while a caller could still create or operate its workload.
Direct kubectl callers and old dispatcher versions can bypass this protocol.
Session workspace cleanup is manual and must happen only after archiving a task
and stopping its workloads. The Job TTL does not clean session directories.

## Org configuration

Everything org-wide lives outside `~/.claude`, so users keep their own settings
and sessions there.

| What | Source | Installed at |
|---|---|---|
| Managed settings (enable the `org` plugin) | `apps/claude-base/managed-settings.json` | `/etc/claude-code/managed-settings.json` |
| Org instructions | `apps/claude-base/CLAUDE.md` | `/etc/claude-code/CLAUDE.md` |
| Plugin marketplace | `apps/claude-base/org/` | `/opt/claude-org/` |
| Agents | `agents/*.md` | `org` plugin, e.g. `org:writer` |
| Skills | `skills/*/SKILL.md` | `org` plugin |
| MCP servers | `apps/claude-base/org/plugins/org/.mcp.json` | `org` plugin, e.g. `plugin:org:lab` |

All of it is in `claude-base`, so the IDE and Jobs get the same configuration.
The `code-server` image also removes VS Code's built-in Copilot Chat extension
and sets `chat.disableAIFeatures`, so Claude Code is the only assistant.

## Storage and limits

Each user has a 10 GiB ReadWriteOnce PVC. Required pod affinity pins a user's
Jobs and session IDEs to their code-server pod's node, so keep it running. Each
IDE pod uses more of the lab VM's 4 GiB memory.

Users have their own PVC and commit identity but share the Claude, Jira, and
GitHub credentials and the Unix uid. This separates working state, not access. Organizational configuration comes from
the images; mutable Claude settings are now per session. CLI and extension
versions can still differ. Editor settings and state persist on the user's PVC.

The lab VM's 60 GiB disk is sparse. Repeated image builds and retained multi-repo
workspaces consume disk; monitor usage and archive unused sessions deliberately.

## Validation

An end-to-end check requires the running lab: create two
sessions, confirm their files differ, resume one, open its IDE, verify automated
turns are rejected while it is open, close it, and resume automation.

## Related

- [ace](https://github.com/brandonros/ace): research and background behind
  this lab (agent command centers, background coding agents, sandboxes).
- [claude-in-a-box](https://github.com/brandonros/claude-in-a-box) (archived):
  the earlier approach, Claude Code behind Open WebUI through an OpenAI-compatible
  wrapper. Replaced here by the real Claude Code extension in code-server.
