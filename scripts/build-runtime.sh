#!/usr/bin/env bash
# Build a runtime image inside Lima, then import it into k3s.
set -euo pipefail

usage() {
    echo "Usage: $0 {server|runner} TAG" >&2
}

if [[ $# -ne 2 ]]; then
    usage
    exit 2
fi
component="$1"
tag="$2"
case "$component" in
    server|runner) ;;
    *) usage; exit 2 ;;
esac
if [[ ! "$tag" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
    echo "Invalid image tag: $tag" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vm="chat"
image="claude-runtime-$component:$tag"
local_context="$(mktemp -d)"
remote_context=""

cleanup() {
    rm -rf -- "$local_context"
    if [[ -n "$remote_context" ]]; then
        limactl shell "$vm" rm -rf -- "$remote_context" ||
            echo "Could not remove VM build directory: $remote_context" >&2
    fi
}
trap cleanup EXIT

# Send only the inputs required by the selected Dockerfile.
sources=("apps/claude-runtime-$component")
if [[ "$component" == runner ]]; then
    sources+=(skills agents)
fi
echo "Preparing build context for $image"
COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' \
    -czf "$local_context/context.tar.gz" -C "$repo_root" "${sources[@]}"

remote_context="$(limactl shell "$vm" mktemp -d /tmp/claude-runtime-build.XXXXXX)"
limactl copy "$local_context/context.tar.gz" "$vm:$remote_context/context.tar.gz"

# A literal script keeps host variables out of remote shell code.
# Values are passed as arguments; pipefail catches docker-save failures.
limactl shell "$vm" bash -s -- "$remote_context" "$component" "$image" <<'REMOTE'
set -euo pipefail
context="$1"
component="$2"
image="$3"

tar -xzf "$context/context.tar.gz" -C "$context"
rm "$context/context.tar.gz"

echo "Building $image"
sudo docker build \
    --tag "$image" \
    --file "$context/apps/claude-runtime-$component/Dockerfile" \
    "$context"

echo "Importing $image into k3s"
sudo docker save "$image" | sudo k3s ctr images import -
REMOTE

echo "Built and imported $image. Update Helm values to deploy this tag."
