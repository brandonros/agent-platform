#!/usr/bin/env bash
# Run through just bootstrap to load .env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
for command in limactl helmfile helm just kubectl python3 openssl; do
    command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
: "${CLAUDE_CODE_OAUTH_TOKEN:?Set CLAUDE_CODE_OAUTH_TOKEN in .env or your environment}"
just init
if ! kubectl -n chat get secret temporal-webui-login >/dev/null 2>&1; then
    : "${OWUI_EMAIL:?Set OWUI_EMAIL for the initial WebUI administrator}"
    : "${OWUI_PASSWORD:?Set OWUI_PASSWORD for the initial WebUI administrator}"
fi
limactl shell chat sudo sh -c 'command -v docker >/dev/null || { apt-get update && apt-get install -y docker.io; }'
just secrets

# Content-derived tags keep reruns stable while rebuilding changed source.
image_tag() {
    python3 - "$@" <<'PY'
import hashlib,sys
from pathlib import Path
digest = hashlib.sha256()
for folder in sys.argv[1:]:
    for file in sorted(Path(folder).rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts:
            digest.update(str(file).encode())
            digest.update(file.read_bytes())
print("bootstrap-" + digest.hexdigest()[:16])
PY
}
export SERVER_IMAGE_TAG="$(image_tag apps/claude-runtime-server)"
export RUNNER_IMAGE_TAG="$(image_tag apps/claude-runtime-runner agents skills)"
just build-runtime server "$SERVER_IMAGE_TAG"
just build-runtime runner "$RUNNER_IMAGE_TAG"
printf 'server: %s\nrunner: %s\n' "$SERVER_IMAGE_TAG" "$RUNNER_IMAGE_TAG" > .runtime-images.yaml

# Configure WebUI before workers start using it.
helmfile -l 'name!=temporal-draft-review-worker,name!=temporal-meeting-notes-worker' sync
kubectl -n chat rollout status deployment/open-webui --timeout=600s
port="${BOOTSTRAP_WEBUI_PORT:-18080}"
kubectl -n chat port-forward service/open-webui "$port:8080" >/dev/null 2>&1 &
forward_pid=$!
trap 'kill "$forward_pid" 2>/dev/null || true; wait "$forward_pid" 2>/dev/null || true' EXIT
export OWUI_URL="http://127.0.0.1:$port"
python3 - <<'PY'
import os,time,urllib.request
for attempt in range(60):
    try:
        with urllib.request.urlopen(os.environ["OWUI_URL"]+"/health",timeout=2) as response:
            if response.status==200: break
    except OSError: pass
    time.sleep(1)
else: raise SystemExit("WebUI port-forward did not become ready")
PY
kill -0 "$forward_pid"
python3 scripts/bootstrap-webui.py
helmfile -l name=temporal-draft-review-worker -l name=temporal-meeting-notes-worker sync
echo "Bootstrap complete. Run:"
echo '  export WORKFLOW_API_TOKEN="$(just api-key)"'
echo '  python3 examples/workflow-clients/meeting_notes.py "Sam owns release notes, due Friday." --wait'
echo "Image tags: SERVER_IMAGE_TAG=$SERVER_IMAGE_TAG RUNNER_IMAGE_TAG=$RUNNER_IMAGE_TAG"
