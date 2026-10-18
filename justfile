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
    if limactl list --quiet | grep -x chat >/dev/null; then
      limactl start --tty=false chat
    else
      limactl start --tty=false --name=chat lima/k3s.yaml
    fi
    umask 077
    limactl shell chat sudo cat /etc/rancher/k3s/k3s.yaml > "$KUBECONFIG"

# Store the Claude token and Entra ID app credentials; keeps the generated cookie secret.
# Reads CLAUDE_CODE_OAUTH_TOKEN and ENTRA_TENANT_ID/ENTRA_CLIENT_ID/ENTRA_CLIENT_SECRET (env or .env).
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
      { kubectl -n chat get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null || true; } | base64 -d
    }
    # Values go through files, not argv, so they stay out of the process list.
    kubectl -n chat create secret generic chat-secrets \
      --from-file=oauth-token=<(printf %s "$token") \
      --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
    : "${ENTRA_TENANT_ID:?Set ENTRA_TENANT_ID in .env}" "${ENTRA_CLIENT_ID:?Set ENTRA_CLIENT_ID in .env}" "${ENTRA_CLIENT_SECRET:?Set ENTRA_CLIENT_SECRET in .env}"
    cookie="$(existing oauth2-proxy cookie-secret)"
    kubectl -n chat create secret generic oauth2-proxy \
      --from-file=tenant-id=<(printf %s "$ENTRA_TENANT_ID") \
      --from-file=client-id=<(printf %s "$ENTRA_CLIENT_ID") \
      --from-file=client-secret=<(printf %s "$ENTRA_CLIENT_SECRET") \
      --from-file=cookie-secret=<(printf %s "${cookie:-$(openssl rand -base64 32 | tr -- '+/' '-_')}") \
      --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -
    kubectl -n chat rollout restart deployment/oauth2-proxy 2>/dev/null || true

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

# Run a Claude Code turn as a Job in the code-server workspace. Extra args: --session UUID, --turn-id ID, --started-by WHO, --wait.
claude-job prompt *args:
    @python3 scripts/claude-job.py "$@"

# List Claude Code Jobs with their sessions and who started them.
claude-jobs:
    kubectl -n chat get jobs -l app=claude-job -L claude-session -L claude-started-by

# Open code-server through Entra ID sign-in at http://localhost:8080 (localhost is a secure context).
code-server:
    while true; do kubectl -n chat port-forward svc/oauth2-proxy 8080:4180; sleep 1; done

logs app="code-server":
    kubectl -n chat logs -f "deployment/$1"

# Remove Helm releases; volumes and the Secret remain.
destroy:
    helmfile destroy

# Build and import an image into the lab VM. Use a new tag for changes.
build-image component tag:
    @bash scripts/build-image.sh "$1" "$2"

# Initialize the lab, build the code-server image, and deploy everything.
bootstrap:
    @bash scripts/bootstrap.sh
