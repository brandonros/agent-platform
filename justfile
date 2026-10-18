set positional-arguments
set dotenv-load
export KUBECONFIG := justfile_directory() / ".kubeconfig"

default:
    @just --list

# Create/start the VM and write its kubeconfig.
init:
    #!/usr/bin/env bash
    set -eu
    helmfile init
    if limactl list --quiet | grep -x agent-platform >/dev/null; then
      limactl start --tty=false agent-platform
    else
      limactl start --tty=false --name=agent-platform lima/k3s.yaml
    fi
    umask 077
    limactl shell agent-platform sudo cat /etc/rancher/k3s/k3s.yaml > "$KUBECONFIG"

# Store the Claude token and, when set, Entra ID app credentials; keeps the generated cookie secret.
# Reads CLAUDE_CODE_OAUTH_TOKEN and optional ENTRA_TENANT_ID/ENTRA_CLIENT_ID/ENTRA_CLIENT_SECRET (env or .env).
secrets:
    #!/usr/bin/env bash
    set -euo pipefail
    token="${CLAUDE_CODE_OAUTH_TOKEN:-}"
    if [ -z "$token" ]; then
      read -rsp "Token from 'claude setup-token': " token
      echo
    fi
    [ -n "$token" ] || { echo "No token given" >&2; exit 1; }
    kubectl create namespace agent-platform --dry-run=client -o yaml | kubectl apply -f -
    # Empty when the Secret or key doesn't exist yet (first run).
    existing() {
      { kubectl -n agent-platform get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null || true; } | base64 -d
    }
    # Values go through files, not argv, so they stay out of the process list.
    kubectl -n agent-platform create secret generic claude-secrets \
      --from-file=oauth-token=<(printf %s "$token") \
      --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
    if [ -z "${ENTRA_TENANT_ID:-}" ] || [ -z "${ENTRA_CLIENT_ID:-}" ] || [ -z "${ENTRA_CLIENT_SECRET:-}" ]; then
      echo "Entra ID not configured (ENTRA_* unset); oauth2-proxy will not be deployed." >&2
      exit 0
    fi
    cookie="$(existing oauth2-proxy cookie-secret)"
    kubectl -n agent-platform create secret generic oauth2-proxy \
      --from-file=tenant-id=<(printf %s "$ENTRA_TENANT_ID") \
      --from-file=client-id=<(printf %s "$ENTRA_CLIENT_ID") \
      --from-file=client-secret=<(printf %s "$ENTRA_CLIENT_SECRET") \
      --from-file=cookie-secret=<(printf %s "${cookie:-$(openssl rand -base64 32 | tr -- '+/' '-_')}") \
      --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
    kubectl -n agent-platform rollout restart deployment/oauth2-proxy 2>/dev/null || true

# Install Traefik, then deploy the apps. oauth2-proxy only when its Secret exists (Entra configured).
apply:
    #!/usr/bin/env bash
    set -eu
    kubectl -n agent-platform get secret claude-secrets >/dev/null 2>&1 ||
      { echo "Run 'just secrets' first" >&2; exit 1; }
    if kubectl -n agent-platform get secret oauth2-proxy >/dev/null 2>&1; then
      helmfile sync
    else
      helmfile -l 'name!=oauth2-proxy' sync
    fi

diff:
    helmfile diff

status:
    kubectl -n agent-platform get pods,pvc,ingress

# Pod memory and CPU use, and the guest's overall memory.
top:
    kubectl top pods -A --sort-by=memory
    limactl shell agent-platform free -h

# Stop the VM and return its memory to macOS; `just init` starts it again.
stop:
    limactl stop agent-platform

# Run a Claude Code turn as a Job in the code-server workspace. Extra args: --session UUID, --turn-id ID, --started-by WHO, --wait.
claude-job prompt *args:
    @python3 scripts/claude-job.py "$@"

# List Claude Code Jobs with their sessions and who started them.
claude-jobs:
    kubectl -n agent-platform get jobs -l app=claude-job -L claude-session -L claude-started-by

# Open code-server at http://localhost:8080 (localhost is a secure context): through Entra ID
# sign-in when oauth2-proxy is deployed, otherwise directly.
code-server:
    #!/usr/bin/env bash
    if kubectl -n agent-platform get svc oauth2-proxy >/dev/null 2>&1; then target=svc/oauth2-proxy:4180; else target=svc/code-server:8080; fi
    echo "Forwarding http://localhost:8080 -> ${target%:*}"
    while true; do kubectl -n agent-platform port-forward "${target%:*}" "8080:${target##*:}"; sleep 1; done

logs app="code-server":
    kubectl -n agent-platform logs -f "deployment/$1"

# Remove Helm releases; volumes and the Secret remain.
destroy:
    helmfile destroy

# Build and import an image into the lab VM. Use a new tag for changes.
build-image component tag:
    @bash scripts/build-image.sh "$1" "$2"

# Initialize the lab, build the code-server image, and deploy everything.
bootstrap:
    @bash scripts/bootstrap.sh
