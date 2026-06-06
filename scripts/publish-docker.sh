#!/usr/bin/env bash
# Build and push the OrionBelt Analytics Docker image to Docker Hub.
#
# Prerequisites:
#   1. Export a Docker Hub PAT: export DOCKERHUB_RALFORION_PAT=...
#   2. A multi-arch builder:    docker buildx create --use --name oba-builder  (first time only)
#
# Usage:
#   ./scripts/publish-docker.sh              # tags :<version> and :latest
#   IMAGE=myorg/oba ./scripts/publish-docker.sh
#   PLATFORMS=linux/amd64 ./scripts/publish-docker.sh
#   SKIP_LATEST=1 ./scripts/publish-docker.sh

set -euo pipefail

IMAGE="${IMAGE:-ralforion/orionbelt-analytics}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
DOCKERHUB_USER="${DOCKERHUB_USER:-ralforion}"

if [[ -z "${DOCKERHUB_RALFORION_PAT:-}" ]]; then
    echo "error: DOCKERHUB_RALFORION_PAT is not set" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

echo "${DOCKERHUB_RALFORION_PAT}" | docker login -u "${DOCKERHUB_USER}" --password-stdin

VERSION="$(grep -E '^__version__' src/__init__.py | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ -z "${VERSION}" ]]; then
    echo "error: could not read __version__ from src/__init__.py" >&2
    exit 1
fi

TAG_ARGS=( --tag "${IMAGE}:${VERSION}" )
if [[ "${SKIP_LATEST:-0}" != "1" ]]; then
    TAG_ARGS+=( --tag "${IMAGE}:latest" )
fi

if ! git diff --quiet HEAD -- . ':!scripts/publish-docker.sh' 2>/dev/null; then
    echo "warning: working tree has uncommitted changes — published image will include them." >&2
fi

echo "Publishing ${IMAGE}:${VERSION}$( [[ "${SKIP_LATEST:-0}" != "1" ]] && echo ' (and :latest)') for ${PLATFORMS}"
echo

docker buildx build \
    --platform "${PLATFORMS}" \
    "${TAG_ARGS[@]}" \
    --push \
    "${REPO_ROOT}"

echo
echo "Pushed ${IMAGE}:${VERSION}$( [[ "${SKIP_LATEST:-0}" != "1" ]] && echo ' and :latest')"
