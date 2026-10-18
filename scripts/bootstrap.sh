#!/usr/bin/env bash
# Run through just bootstrap to load .env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
for command in limactl helmfile helm just kubectl openssl python3; do
    command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
: "${CLAUDE_CODE_OAUTH_TOKEN:?Set CLAUDE_CODE_OAUTH_TOKEN in .env or your environment}"
just init
limactl shell chat sudo sh -c 'command -v docker >/dev/null || { apt-get update && apt-get install -y docker.io; }'
just secrets
tag_of() { sed -n "s/^$1: *\"\{0,1\}\([^\"]*\)\"\{0,1\}\$/\1/p" image-tags.yaml; }
just build-image claude-base "${CLAUDE_BASE_IMAGE_TAG:-$(tag_of claude-base)}"
just build-image code-server "${CODE_SERVER_IMAGE_TAG:-$(tag_of code-server)}"
helmfile sync
echo "Bootstrap complete. Open code-server: just code-server (http://localhost:8080)"
