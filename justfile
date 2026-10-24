set positional-arguments
set dotenv-load
export KUBECONFIG := justfile_directory() / ".kubeconfig"

default:
    @just --list

# First-time setup: VM, secrets, both images, releases.
bootstrap: init secrets (build-image "claude-base") (build-image "code-server") apply

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

# Store CLAUDE_CODE_OAUTH_TOKEN, Jira and GitHub credentials and git identity when set, and, when ENTRA_* are set, oauth2-proxy credentials (keeping its cookie secret).
secrets:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${CLAUDE_CODE_OAUTH_TOKEN:?set it in .env (from 'claude setup-token')}"
    # Values go through files, not argv, so they stay out of the process list.
    secret() { kubectl -n agent-platform create secret generic "$@" --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f -; }
    kubectl create namespace agent-platform --dry-run=client -o yaml | kubectl apply -f -
    secret claude-secrets --from-file=oauth-token=<(printf %s "$CLAUDE_CODE_OAUTH_TOKEN")
    if [ -n "${JIRA_BASE_URL:-}" ] && [ -n "${JIRA_EMAIL:-}" ] && [ -n "${JIRA_API_TOKEN:-}" ]; then
      secret jira \
        --from-file=JIRA_BASE_URL=<(printf %s "$JIRA_BASE_URL") \
        --from-file=JIRA_EMAIL=<(printf %s "$JIRA_EMAIL") \
        --from-file=JIRA_API_TOKEN=<(printf %s "$JIRA_API_TOKEN")
    else
      echo "Jira not configured (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN); Command Center shows demo tickets." >&2
    fi
    if [ -n "${GH_TOKEN:-}" ]; then
      secret github --from-file=GH_TOKEN=<(printf %s "$GH_TOKEN")
    else
      echo "GitHub not configured (GH_TOKEN); git push and gh will fail." >&2
    fi
    # Env from these Secrets is read at pod start.
    kubectl -n agent-platform rollout restart deployment -l app=code-server 2>/dev/null || true
    if [ -z "${ENTRA_TENANT_ID:-}" ] || [ -z "${ENTRA_CLIENT_ID:-}" ] || [ -z "${ENTRA_CLIENT_SECRET:-}" ]; then
      echo "Entra ID not configured (ENTRA_* unset); oauth2-proxy will not be deployed." >&2
      exit 0
    fi
    cookie="$({ kubectl -n agent-platform get secret oauth2-proxy -o 'jsonpath={.data.cookie-secret}' 2>/dev/null || true; } | base64 -d)"
    secret oauth2-proxy \
      --from-file=tenant-id=<(printf %s "$ENTRA_TENANT_ID") \
      --from-file=client-id=<(printf %s "$ENTRA_CLIENT_ID") \
      --from-file=client-secret=<(printf %s "$ENTRA_CLIENT_SECRET") \
      --from-file=cookie-secret=<(printf %s "${cookie:-$(openssl rand -base64 32 | tr -- '+/' '-_')}")
    kubectl -n agent-platform rollout restart deployment/oauth2-proxy 2>/dev/null || true

# Build an image in the VM and import it into k3s, tagged from image-tags.yaml. Bump the tag for changes.
build-image component:
    #!/usr/bin/env bash
    set -euo pipefail
    tag() { awk -F'"' "/^$1:/ {print \$2}" image-tags.yaml; }
    image="$1:$(tag "$1")"
    COPYFILE_DISABLE=1 tar --no-xattrs --exclude=__pycache__ --exclude=node_modules --exclude=out --exclude="*.vsix" -cz apps agents skills |
      limactl shell --workdir / agent-platform sudo docker build -t "$image" \
        --build-arg "BASE_TAG=$(tag claude-base)" -f "apps/$1/Dockerfile" -
    limactl shell --workdir / agent-platform sh -c "sudo docker save $image | sudo k3s ctr images import -"

# Deploy the releases; oauth2-proxy only when its Secret exists (Entra configured).
apply:
    kubectl -n agent-platform get secret claude-secrets >/dev/null || { echo "Run 'just secrets' first" >&2; exit 1; }
    helmfile $(kubectl -n agent-platform get secret oauth2-proxy >/dev/null 2>&1 || echo "-l name!=oauth2-proxy") sync

diff:
    helmfile diff

status:
    kubectl -n agent-platform get pods,pvc

# Pod memory and CPU use, and the guest's overall memory.
top:
    kubectl top pods -A --sort-by=memory
    limactl shell agent-platform free -h

# Stop the VM and return its memory to macOS; `just init` starts it again.
stop:
    limactl stop agent-platform

# Run a turn in its persistent session workspace. Extra args: --session UUID, --turn-id ID, --started-by WHO, --wait.
claude-job prompt *args:
    @python3 scripts/claude-job.py "$@"

# List Claude Code Jobs with their sessions and who started them.
claude-jobs:
    kubectl -n agent-platform get jobs -l app=claude-job -L claude-session -L claude-started-by

# Open a user's code-server (users.yaml) at http://localhost:<port> (localhost is a secure
# context): through Entra ID sign-in when oauth2-proxy is deployed, otherwise directly.
code-server user port="8080":
    #!/usr/bin/env bash
    if kubectl -n agent-platform get svc oauth2-proxy >/dev/null 2>&1; then target=svc/oauth2-proxy:4180; else target="svc/code-server-$1:8080"; fi
    echo "Forwarding http://localhost:$2 -> ${target%:*}"
    while true; do kubectl -n agent-platform port-forward "${target%:*}" "$2:${target##*:}"; sleep 1; done

logs app:
    kubectl -n agent-platform logs -f "deployment/$1"

# Remove Helm releases; volumes and the Secret remain.
destroy:
    helmfile destroy

# Open a session-specific IDE; Ctrl-C stops forwarding but retains ownership.
session-open session:
    @python3 scripts/claude-session.py open "$1"

# Stop the session IDE and allow automated turns again; files are retained.
session-close session:
    @python3 scripts/claude-session.py close "$1"
