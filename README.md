# chat-poc

Organizational Claude Code in a local Kubernetes lab. People use the real Claude
Code (VS Code extension) in a browser through code-server. Automated work runs
as Kubernetes Jobs in the same workspace, so its sessions appear in the same
Session Manager. Org agents, skills, MCP servers, and instructions are baked
into the image as managed configuration.

## Components

| App | Helm values | Purpose |
|---|---|---|
| `apps/claude-base/` | (image only) | `claude-base` image: Claude Code CLI, git, python3, org config. Jobs run it |
| `apps/code-server/` | `values/code-server.yaml.gotmpl` | `code-server` image, built `FROM claude-base`: code-server + Claude Code extension |
| `apps/mcp-test-server/` | `values/mcp-test-server.yaml.gotmpl` | Cluster MCP fixture: `echo` tool and `echo_test` prompt |
| (Job template) | `values/claude-job.yaml.gotmpl` | `claude-job-template` ConfigMap: the Job spec for one Claude Code turn (`claude-base` image) |
| `scripts/claude-job.py` | (none) | Dispatches a turn: fills the template's name, prompt, and session |
| (upstream) | `values/oauth2-proxy.yaml.gotmpl` | Entra ID sign-in in front of code-server |
| (upstream) | `values/traefik.yaml` | Traefik ingress |

## Deploy

Requires Lima 2+, Helmfile, Helm, just, kubectl, Python 3, and OpenSSL, plus a
Microsoft Entra ID app registration for sign-in:

1. Entra admin center → App registrations → New registration. Single tenant.
   Redirect URI: platform **Web**, `http://localhost:8080/oauth2/callback`.
2. Certificates & secrets → New client secret.
3. Copy `.env.example` to `.env` and set `CLAUDE_CODE_OAUTH_TOKEN`,
   `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID` (application ID), and `ENTRA_CLIENT_SECRET`.

```bash
just bootstrap
just code-server          # http://localhost:8080, sign in with Entra ID
```

code-server runs with `--auth none` and has no ingress; the only way in is
oauth2-proxy, which signs people in with Entra ID and proxies to code-server.
Any account in the tenant may sign in; restrict it with `OAUTH2_PROXY_ALLOWED_GROUPS`
(group object IDs) in `values/oauth2-proxy.yaml.gotmpl`.

Use `localhost`: browsers block service workers and webviews (including the
Claude Code panel) on plain-HTTP hosts, and the Entra redirect URI is registered
for `localhost`. `kubectl port-forward` exits when a connection breaks, so the
recipe restarts it.

Two images, tagged in `image-tags.yaml`. `code-server` is built `FROM
claude-base:<claude-base tag>`, so after changing org config, agents, skills, or
the CLI, rebuild both:

```bash
just build-image claude-base 2
# set claude-base: "2" in image-tags.yaml
just build-image code-server 8
# set code-server: "8" in image-tags.yaml
helmfile -l name=code-server -l name=claude-job sync
```

For editor-only changes (`apps/code-server/Dockerfile`), rebuild only
`code-server`. `CLAUDE_BASE_IMAGE_TAG` and `CODE_SERVER_IMAGE_TAG` override
`image-tags.yaml` for one build or sync.

The VS Code extension bundles its own Claude Code, so IDE sessions and Jobs can
run slightly different Claude Code versions; they share the same session files.

## Running turns as Jobs

```bash
just claude-job "Summarize the README" --wait                          # new session
just claude-job "Now shorten it" --session <session-uuid> --wait       # continue it
just claude-jobs                                                       # list Jobs and sessions
```

Each turn is a Job built from the `claude-job-template` ConfigMap, which Helm
installs from `values/claude-job.yaml.gotmpl`; change limits, timeouts, or the
command there. Jobs run the `claude-base` image on code-server's workspace volume
(`~/workspace`, `~/.claude`) as a separate release. A Job runs `claude -p` in `~/workspace` with
`bypassPermissions`, `--session-id` for a new session or `--resume` when the
session file exists, and `CLAUDE_CODE_ENTRYPOINT=claude-vscode` so the
extension's Session Manager lists it. People can open and continue those
sessions in the browser.

- The result is Claude Code's JSON output, the last line of the Job's log.
  `--wait` prints it. Jobs are deleted 24 hours after finishing.
- `--turn-id` names the Job after that ID. Dispatching the same turn again
  reuses the existing Job instead of running the prompt twice.
- Limits per Job: 1 hour, 1 GiB memory, no retries.
- Don't run two turns of one session at once, or type into a session while a
  Job is running it; nothing serializes them.

Anything that can create a Job can trigger work: the script, a CronJob, CI, or a
workflow engine. The volume is ReadWriteOnce, which works because the lab has
one node; with several nodes, Jobs must run on code-server's node or the volume
must be ReadWriteMany.

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

## Storage

One volume holds `~/workspace` and `~/.claude`. Claude Code sessions are
`~/.claude/projects/<encoded-folder>/<session-id>.jsonl`; the extension, the
`claude` CLI, and Jobs share them. Settings under `~/.local/share/code-server`
are in the image, not the volume, so browser-side settings changes reset on
restart.

The lab VM's 60 GiB disk is a sparse file on the Mac. If the Mac's disk fills,
the VM reports I/O errors (image imports fail). Docker build copies of images
duplicate what k3s holds; `sudo docker image prune -a` and `sudo fstrim /` inside
the VM return that space.

## Limits

One shared code-server instance behind Entra ID sign-in, with one shared Claude
token: everyone who signs in uses the same workspace and identity inside it.
The lab VM has 4 GiB of memory. Per-user workspaces (for example Coder) and
trusted HTTPS are not set up.
