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
    if limactl list --quiet | grep -x chat >/dev/null; then
      limactl start --tty=false chat
    else
      limactl start --tty=false --name=chat lima/k3s.yaml
    fi
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

logs app="claude-runtime-server":
    kubectl -n chat logs -f "deployment/$1"

# Remove Helm releases; volumes and the Secret remain.
destroy:
    helmfile destroy


# Build and import a runtime image into the lab VM. Use a new tag for changes.
build-runtime component tag:
    @bash scripts/build-runtime.sh "$1" "$2"

# Initialize the lab, build images, configure WebUI, and deploy workers.
bootstrap:
    @bash scripts/bootstrap.sh
