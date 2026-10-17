set positional-arguments
set dotenv-load
export KUBECONFIG := justfile_directory() / ".kubeconfig"

default:
    @just --list

# Deploy only the official SDK service; does not change the existing UI/workflows.
sdk-apply:
    helmfile -l name=claude-runtime-server sync

# Install/update the WebUI extension (OWUI_EMAIL and OWUI_PASSWORD required).
sdk-pipe:
    python3 scripts/sync_pipe.py

# Create/start the VM and write its kubeconfig.
init:
    #!/usr/bin/env bash
    set -eu
    helmfile init
    limactl start --tty=false --name=chat lima/k3s.yaml
    umask 077
    limactl shell chat sudo cat /etc/rancher/k3s/k3s.yaml > "$KUBECONFIG"

# Store the Claude token; keeps existing generated keys. Reads CLAUDE_CODE_OAUTH_TOKEN (env or .env), else prompts.
secrets:
    #!/usr/bin/env bash
    set -euo pipefail
    token="${CLAUDE_CODE_OAUTH_TOKEN:-}"
    if [ -z "$token" ]; then
      read -rsp "Token from 'claude setup-token': " token
      echo
    fi
    [ -n "$token" ] || { echo "No token given" >&2; exit 1; }
    kubectl create namespace chat --dry-run=client -o yaml | kubectl apply -f -
    # Empty when the Secret or key doesn't exist yet (first run).
    existing() {
      { kubectl -n chat get secret chat-secrets -o "jsonpath={.data.$1}" 2>/dev/null || true; } | base64 -d
    }
    api_key="$(existing api-key)"
    webui_key="$(existing webui-secret-key)"
    # Values go through files, not argv, so they stay out of the process list.
    kubectl -n chat create secret generic chat-secrets \
      --from-file=oauth-token=<(printf %s "$token") \
      --from-file=api-key=<(printf %s "${api_key:-$(openssl rand -hex 32)}") \
      --from-file=webui-secret-key=<(printf %s "${webui_key:-$(openssl rand -hex 32)}") \
      --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
    # Env from Secrets is read at start; restart so a new token takes effect.
    if kubectl -n chat get deployment claude-runtime-server >/dev/null 2>&1; then
      kubectl -n chat rollout restart deployment/claude-runtime-server
    fi

# Install Traefik, then deploy the apps.
apply:
    #!/usr/bin/env bash
    set -eu
    kubectl -n chat get secret chat-secrets >/dev/null 2>&1 ||
      { echo "Run 'just secrets' first" >&2; exit 1; }
    helmfile sync

diff:
    helmfile diff

status:
    kubectl -n chat get pods,pvc,ingress

# Pod memory and CPU use, and the guest's overall memory.
top:
    kubectl top pods -A --sort-by=memory
    limactl shell chat free -h

# Stop the VM and return its memory to macOS; `just init` starts it again.
stop:
    limactl stop chat

# Print the lab service bearer key.
api-key:
    @kubectl -n chat get secret chat-secrets -o jsonpath='{.data.api-key}' | base64 -d; echo

# Start the WebUI workflow; prints its ID.
run topic:
    #!/usr/bin/env bash
    set -eu
    id="draft-$(date +%Y%m%d-%H%M%S)"
    input="$(python3 -c 'import json,sys; print(json.dumps({"topic": sys.argv[1]}))' "$1")"
    kubectl -n chat exec deploy/temporal-dev-server -- temporal workflow start \
      --type DraftReviewWebUI --task-queue draft-review-webui --workflow-id "$id" --input "$input" >/dev/null
    echo "$id"
    echo "Watch: http://temporal.127.0.0.1.sslip.io/namespaces/default/workflows/$id"

# State, critiques, and latest version of a workflow.
show id:
    @kubectl -n chat exec deploy/temporal-dev-server -- temporal workflow query \
      --workflow-id "$1" --type status --output json \
      | python3 -c 'import json,sys; s=json.load(sys.stdin)["queryResult"][0]; has_latest="latest" in s; latest=s.pop("latest", None); print(json.dumps(s, indent=2)); print("\n--- latest version ---\n" + (latest or "(none yet)")) if has_latest else None'

approve id:
    kubectl -n chat exec deploy/temporal-dev-server -- temporal workflow signal \
      --workflow-id "$1" --name submit_review --input '"approve"'

# Ask for changes; the workflow revises and waits for review again.
revise id note:
    #!/usr/bin/env bash
    set -eu
    note="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$2")"
    kubectl -n chat exec deploy/temporal-dev-server -- temporal workflow signal \
      --workflow-id "$1" --name submit_review --input '"revise"' --input "$note"

reject id:
    kubectl -n chat exec deploy/temporal-dev-server -- temporal workflow signal \
      --workflow-id "$1" --name submit_review --input '"reject"'

logs app="claude-runtime-server":
    kubectl -n chat logs -f "deployment/$1"

# Remove Helm releases; volumes and the Secret remain.
destroy:
    helmfile destroy


# Build either runtime image in the Lima VM and import it into k3s. Use a new tag for changes.
build-runtime component tag:
    #!/usr/bin/env bash
    set -euo pipefail
    case "$1" in server|runner) ;; *) echo 'component must be server or runner' >&2; exit 1;; esac
    [[ "$2" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$ ]] || exit 1
    COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' -czf - "apps/claude-runtime-$1" skills agents | \
      limactl shell chat sh -c 'set -eu; context=$(mktemp -d); trap '\''rm -rf "$context"'\'' EXIT; tar -xzf - -C "$context"; sudo docker build -t "claude-runtime-$1:$2" -f "$context/apps/claude-runtime-$1/Dockerfile" "$context"; sudo docker save "claude-runtime-$1:$2" | sudo k3s ctr images import -' -- "$1" "$2"

# Agent + skill + MCP + structured decision, all through one saved WebUI chat.
capabilities:
    #!/usr/bin/env bash
    set -euo pipefail
    id="capabilities-$(date +%Y%m%d-%H%M%S)"
    kubectl -n chat exec deploy/temporal-dev-server -- temporal workflow start \
      --type AgentCapabilities --task-queue draft-review-webui --workflow-id "$id"
    echo "Inspect with: just show $id"

# Extract meeting outcomes and route missing details to clarification.
meeting notes:
    #!/usr/bin/env bash
    set -euo pipefail
    id="meeting-$(date +%Y%m%d-%H%M%S)"
    input="$(python3 -c 'import json,sys; print(json.dumps({"notes": sys.argv[1]}))' "$1")"
    kubectl -n chat exec deploy/temporal-dev-server -- temporal workflow start \
      --type MeetingNotes --task-queue meeting-notes --workflow-id "$id" --input "$input"
    echo "Inspect with: just show $id"
