# chat-poc

Organizational Claude Code in Kubernetes, using the official Python Agent SDK.
Open WebUI provides chat; Temporal provides code-driven workflows. Each Claude
turn runs in a Kubernetes Job with persisted session context.

```text
Open WebUI → extension → Claude runtime API → Job → Agent SDK
Temporal WebUI ────────→ Open WebUI → extension → runtime API
```

## Components

App-specific values filenames match their source directories:

| App | Helm values | Deployed release |
|---|---|---|
| `apps/claude-runtime-server/` | `values/claude-runtime-server.yaml.gotmpl` | API Deployment: `claude-runtime-server` |
| `apps/claude-runtime-runner/` | Runner image selected in server values | One Kubernetes Job per turn; no standing Deployment |
| `apps/open-webui-pipe-extension/` | Installed through WebUI's admin API | Runs inside `open-webui` |
| `apps/temporal-draft-review-worker/` | `values/temporal-draft-review-worker.yaml.gotmpl` | `temporal-draft-review-worker` |
| `apps/mcp-test-server/` | `values/mcp-test-server.yaml.gotmpl` | `mcp-test-server` |

`values/open-webui.yaml`, `values/temporal-dev-server.yaml`, and `values/traefik.yaml` deploy
upstream infrastructure. App releases, Services, and runtime storage use the same names as their source directories.

The server accepts runs, stores results/events, and creates Jobs through `jobs.py`.
The runner executes one Agent SDK turn, writes its result, and exits. Their images
and dependencies are separate; they communicate through session files and JSON
run/event/result records. Change that shared format compatibly when releasing either.
Temporal orchestrates business workflow steps; the runtime server manages executions.

## Deploy and use

Requires Lima, Helmfile, Helm, just, kubectl, Python 3, and OpenSSL.

```bash
just init
just secrets                 # Reads CLAUDE_CODE_OAUTH_TOKEN or prompts
just build-runtime server job-template-1
just build-runtime runner agents-1
just apply
OWUI_EMAIL=... OWUI_PASSWORD=... just sdk-pipe
```

The WebUI workflow also requires a Kubernetes Secret named `temporal-webui-login`
with `email` and `password` keys for an existing WebUI account. Its chats belong
to that account. The deployed lab uses the Test account.

- Chat: http://chat.127.0.0.1.sslip.io
- Runtime API/docs: http://agent.127.0.0.1.sslip.io/docs
- Temporal: http://temporal.127.0.0.1.sslip.io

Ask the chat to use the team-glossary skill to explain Bluebird and ACE.
Follow-up messages resume the saved Claude session.

```bash
just run "A staff writing pilot"        # One saved WebUI conversation
just show <workflow-id>                 # State, chat ID, critique, latest draft
just revise <workflow-id> "Shorten it"
just approve <workflow-id>
just reject <workflow-id>
just status
just logs
just stop
```

Rerun `just sdk-pipe` after editing the extension. `just sdk-apply` updates the
runtime server; `just apply` reconciles all releases. Both runtime Dockerfiles use
this repository as build context. `just build-runtime server <tag>` and
`just build-runtime runner <tag>` build separately inside Lima and import into k3s.
The lab VM needs Docker (`sudo apt-get install docker.io`) for these recipes.
For another cluster, build/push the images to its registry and set image references
in the server values. No dependencies are installed when the server or Jobs start.

Use new tags for changes. Set `SERVER_IMAGE_TAG` and `RUNNER_IMAGE_TAG` when running
Helmfile/just to select each independently (server defaults to `job-template-1`; runner to `agents-1`). Changing the
runner tag affects future Jobs; active Jobs keep their original image. The server
loads `profiles.json` from a ConfigMap sourced from the runner directory. Agents,
skills, and CLAUDE.md are baked into the runner image and require rebuilding it.

## Workflow behavior

The workflow drafts, requests a structured critique, revises when `ok` is false,
and waits for a human decision. Temporal parses the result and chooses the next
step in code. A revision limit bounds automatic rounds; the human gate expires
after three days. Approval with outstanding issues is rejected as invalid.

The WebUI workflow (`DraftReviewWebUI`, queue `draft-review-webui`) first creates
one chat and records its ID in Temporal. Every draft, critique, and revision
continues in that chat and underlying Claude session. Activities poll saved
responses and recover using stable message IDs. The extension forwards role and
schema parameters; schema results are saved as JSON text for the worker to parse.
At the human gate, the chat can also be continued manually.

Chat creation recovery uses a unique title because WebUI has no atomic chat-create
idempotency facility. Concurrent creation is not guaranteed safe. Avoid renaming
the chat during creation/recovery, or driving the same conversation simultaneously
through human messages and workflow signals; write arbitration is not implemented.

## Models, agents, skills, and MCP

The WebUI selector exposes Haiku 5.5, Sonnet 5.5, Opus 5.5, and Fable 5.1 with the
same organizational configuration. Native role prompts live in
`agents/*.md`; `profiles.json` references them and
configures tools. Temporal selects roles explicitly. Chat can request configured
specialists through the Agent tool. Skills live in `skills/`; shared instructions
live in `apps/claude-runtime-runner/CLAUDE.md`.

Per-user `EFFORT` settings offer Model default, Low, Medium, High, Extra high,
and Max. Explicit `reasoning_effort` completion parameters override the setting.
Model default leaves the SDK override unset. Runtime callers can supply `effort`;
Temporal's `Turn.effort` is optional. Thinking summaries are displayed only when
provided by the SDK; they are separate from answer text and tool results.

The cluster-only MCP test server provides `mcp__lab__echo`. General and integration
profiles configure its URL programmatically through SDK `mcp_servers`; no
`.mcp.json` is required. Runner network policy permits that server on port 8000.

## Runtime API and storage

Use `Authorization: Bearer <chat-secrets/api-key>`. Trusted adapters assert
`X-Owner-ID`; the bearer credential must stay server-side because it can assert
any owner. WebUI uses the authenticated user ID; direct API callers default to
`service`. Ownership is checked for reads, continuation, and cancellation.

| Endpoint | Purpose |
|---|---|
| `POST /runs` | Submit prompt, model, agent, optional effort/schema/session linkage; use `Idempotency-Key` |
| `GET /runs/{id}` | Status, text result, structured output, error, session ID |
| `GET /runs/{id}/events?after=0` | Replayable SSE events |
| `POST /runs/{id}/cancel` | Interrupt execution, then delete the Job if necessary |

Repeating an identical request key/body reconnects to the existing run. Changed
bodies with reused keys are rejected. The SDK validates `output_schema` through
its structured output feature; successful data is returned as `structured_output`.

WebUI chat/response/parent IDs map to native sessions. Latest-turn continuation
resumes; editing or regenerating an older response forks at its checkpoint.
Copied chats fork within the same owner. Failed or cancelled runs can be continued
when their transcript survived. SSE disconnects alone do not cancel Jobs.

One PVC holds API SQLite data and per-session directories. Each runner mounts
only its session directory, containing `home/.claude/`, `workspace/`, and `runs/`.
The API sees the full volume. Session data survives Job cleanup and API restarts.
Branching copies current workspace files; it is not a historical filesystem snapshot.

This lab has one API replica, at most two active Jobs, and serialized turns per
session. SQLite scheduling and local-path RWO storage do not support simply adding
API replicas or spreading runners across nodes. Jobs have bounded time/turn/budget
limits. Workers have no Kubernetes token or runtime API key; read hooks restrict
access to workspace and organizational files. This is not a hostile-tenant sandbox.

Production work remains around credentials, shared storage, retention/backups,
capacity, pinned images, and multi-user workflow ownership. Attachments and
interactive tool approvals are outside the current chat integration.

## Maintenance and verification

The only setup script is `scripts/sync_pipe.py`, which installs the WebUI pipe.
Agents and skills are packaged in the runner image; no WebUI agent/skill sync is needed.

Run `just capabilities` for the maintained Temporal end-to-end example. Inspect
its state with `just show <workflow-id>` and open its `chat_id` in WebUI.
`AgentCapabilities` runs on the existing worker and queue:
1. Create one WebUI chat and persist its ID.
2. Select the native `writer` agent and invoke `team-glossary`.
3. Continue that chat and invoke MCP echo with a workflow-generated unique nonce.
4. Request a schema-constrained answer containing ACE, nonce, and decision.
5. Validate values and successful tool call/result pairs from saved WebUI output.
   Code routes to `completed` only when all checks pass and the decision is `ready`;
   otherwise it returns `needs_review`. Execution errors follow activity failure handling.

The writer profile explicitly enables the MCP echo server. No direct runtime API
credentials are given to this workflow. Its three turns all go through WebUI.
This replaces the standalone SDK smoke script and incurs model usage.

Local tests retain coverage for retries, ownership/isolation, session branching,
structured output, workflow decisions, and WebUI event formatting. Run the
worker suite separately from the API/extension tests:

```bash
python -m pytest apps/claude-runtime-server/test_server.py apps/open-webui-pipe-extension/test_pipe.py
python -m pytest apps/temporal-draft-review-worker/test_worker.py
```

Use both server and runner requirements plus `pytest`, `temporalio==1.34.0`, and `httpx` in a
local environment. Live acceptance on 2026-10-17 verified worker interruption
and recovery on activity attempt 2 without duplicate runs/messages, one-session
context across all workflow steps, and structured critique routing to revision.
HTTP checks verified separate tool/result/answer items and completion status.
No visual browser certification is claimed; reasoning text was absent in the
latest HTTP check. The one-off acceptance scripts were removed after the experiment.

## Adding agents

`agents/` sits beside `skills/`. Following [Claude Code's native subagent format](https://code.claude.com/docs/en/sub-agents), each agent is a Markdown file
with YAML frontmatter (`name`, `description`, and optional `model: inherit`), then
its role instructions. `agents/meeting-facilitator.md` is a nontechnical example.
Keep the filename stem equal to `name`; the runtime permission hook uses it as
its delegation allowlist. These definitions are baked into `.claude/agents/` in
the runner image. Rebuild the runner and select its new tag after edits.

In one saved chat, ask "Use the writer agent to draft this", then "Use the reviewer
agent to critique the draft", then "Use the meeting-facilitator agent to plan a
meeting about it". Claude delegates using the Agent tool. Each specialist has
its own context; the main conversation passes the relevant task/context and
receives the result. This does not create three independent WebUI chats. Native
agent files do not need entries in `profiles.json` for this conversational use;
profiles are for explicitly selecting the main agent through the API.

## Runner Job configuration

`values/claude-runtime-server.yaml.gotmpl` owns the complete runner Job template:
image, resource limits, execution timeout/deadline, credentials, mounts, security,
retry policy, and cleanup TTL. Helm stores it in the `runner-job` ConfigMap and
mounts `job-template.json` into the server. `jobs.py` loads this template and fills
only `${RUN_ID}` and `${SESSION_ID}` before submitting it. It contains no runner
image, pod settings, or fallback defaults. Change runner settings in the values
file and apply Helm; rebuilding the server is unnecessary for those changes.
