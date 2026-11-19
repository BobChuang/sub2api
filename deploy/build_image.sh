#!/usr/bin/env bash
# 本地构建镜像的快速脚本，避免在命令行反复输入构建参数。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

RELEASE_BUILD=/usr/local/bin/release-build
if [[ -x "$RELEASE_BUILD" && "$(< /proc/self/cgroup)" != *:/releasebuild.slice/* ]]; then
    exec "$RELEASE_BUILD" "$BASH" "${SCRIPT_DIR}/build_image.sh" "$@"
fi
DOCKER=docker
if [[ -x "$RELEASE_BUILD" && -x /usr/local/bin/docker ]]; then
    DOCKER=/usr/local/bin/docker
fi

IMAGE_TAG=${SUB2API_IMAGE_TAG:-sub2api:latest}
IMAGE_ARCHIVE=${SUB2API_IMAGE_ARCHIVE:-}
if [[ -n "$IMAGE_ARCHIVE" && ( -e "$IMAGE_ARCHIVE" || -L "$IMAGE_ARCHIVE" ) ]]; then
    printf 'Image archive already exists: %s\n' "$IMAGE_ARCHIVE" >&2
    exit 1
fi

"$DOCKER" build -t "$IMAGE_TAG" \
    --build-arg GOPROXY=https://goproxy.cn,direct \
    --build-arg GOSUMDB=sum.golang.google.cn \
    -f "${REPO_ROOT}/Dockerfile" \
    "${REPO_ROOT}"

if [[ -n "$IMAGE_ARCHIVE" ]]; then
    archive_tmp=$(mktemp -- "${IMAGE_ARCHIVE}.XXXXXX")
    trap 'rm -f -- "$archive_tmp"' EXIT
    "$DOCKER" image save "$IMAGE_TAG" | gzip -c > "$archive_tmp"
    ln -T -- "$archive_tmp" "$IMAGE_ARCHIVE"
fi
