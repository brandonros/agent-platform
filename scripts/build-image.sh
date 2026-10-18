#!/usr/bin/env bash
# Build an app image inside Lima, then import it into k3s.
set -euo pipefail

usage() {
    echo "Usage: $0 {claude-base|code-server} TAG" >&2
}

if [[ $# -ne 2 ]]; then
    usage
    exit 2
fi
component="$1"
tag="$2"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base_tag=""
case "$component" in
    claude-base) app_dir="apps/claude-base"; sources=("$app_dir" skills agents) ;;
    code-server)
        app_dir="apps/code-server"; sources=("$app_dir")
        # Built FROM claude-base; CLAUDE_BASE_IMAGE_TAG overrides image-tags.yaml.
        base_tag="${CLAUDE_BASE_IMAGE_TAG:-$(sed -n 's/^claude-base: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$repo_root/image-tags.yaml")}" ;;
    *) usage; exit 2 ;;
esac
image_name="$component"
if [[ ! "$tag" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
    echo "Invalid image tag: $tag" >&2
    exit 2
fi

vm="chat"
image="$image_name:$tag"
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
echo "Preparing build context for $image"
COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' \
    -czf "$local_context/context.tar.gz" -C "$repo_root" "${sources[@]}"

remote_context="$(limactl shell "$vm" mktemp -d /tmp/image-build.XXXXXX)"
limactl copy "$local_context/context.tar.gz" "$vm:$remote_context/context.tar.gz"

# A literal script keeps host variables out of remote shell code.
# Values are passed as arguments; pipefail catches docker-save failures.
limactl shell "$vm" bash -s -- "$remote_context" "$app_dir" "$image" "$base_tag" <<'REMOTE'
set -euo pipefail
context="$1"
app_dir="$2"
image="$3"
base_tag="$4"

tar -xzf "$context/context.tar.gz" -C "$context"
rm "$context/context.tar.gz"

echo "Building $image"
sudo docker build \
    --tag "$image" \
    --build-arg "BASE_TAG=$base_tag" \
    --file "$context/$app_dir/Dockerfile" \
    "$context"

echo "Importing $image into k3s"
sudo docker save "$image" | sudo k3s ctr images import -
REMOTE

echo "Built and imported $image."
