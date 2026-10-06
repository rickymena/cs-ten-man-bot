#!/usr/bin/env bash
set -euo pipefail

# Builds the production image and exports it for transfer to the VPS.
#   ./build.sh              -> localhost/tenman-bot:latest, exported to dist/tenman-bot-latest.tar
#   ./build.sh v0.1.0       -> localhost/tenman-bot:v0.1.0 + :latest, exported to dist/tenman-bot-v0.1.0.tar
#   ./build.sh --dev        -> localhost/tenman-bot:dev (test runner), not exported
#
# Works with either engine. Podman is preferred when both are installed;
# override with CONTAINER_ENGINE=docker ./build.sh

IMAGE_NAME="tenman-bot"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${HERE}/dist"

if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
  ENGINE="${CONTAINER_ENGINE}"
elif command -v podman >/dev/null 2>&1; then
  ENGINE="podman"
elif command -v docker >/dev/null 2>&1; then
  ENGINE="docker"
else
  echo "ERROR: neither podman nor docker found on PATH." >&2
  exit 1
fi

if ! "${ENGINE}" info >/dev/null 2>&1; then
  echo "ERROR: '${ENGINE}' is installed but not usable (is the daemon running?)." >&2
  exit 1
fi

# Podman namespaces locally-built images under localhost/; Docker does not.
# The release image is always named localhost/tenman-bot (see below), which
# both compose files reference; these hints are for the --dev message only.
if [[ "${ENGINE}" == "podman" ]]; then
  LOCAL_PREFIX="localhost/"
  COMPOSE_HINT="compose.yaml"
else
  LOCAL_PREFIX=""
  COMPOSE_HINT="compose.docker.yaml"
fi

# Docker builds through buildx; with a docker-container builder selected the
# result stays in the build cache unless --load imports it as a local image.
LOAD_FLAG=()
if [[ "${ENGINE}" == "docker" ]]; then
  LOAD_FLAG=(--load)
fi

echo "==> Engine: ${ENGINE}"

# buildah stages each finished layer as a tar under TMPDIR before committing it.
# On Fedora /var/tmp is a small tmpfs, so a large layer fails there with "no
# space left on device". Keep the staging area on real disk next to the repo.
export TMPDIR="${TMPDIR_OVERRIDE:-${HERE}/.build-tmp}"
mkdir -p "${TMPDIR}"

if [[ "${1:-}" == "--dev" ]]; then
  echo "==> Building dev image: ${IMAGE_NAME}:dev (target: dev)"
  "${ENGINE}" build "${LOAD_FLAG[@]}" --platform linux/amd64 -f "${HERE}/Containerfile" --target dev -t "${IMAGE_NAME}:dev" "${HERE}"
  echo ""
  echo "Run the unit suite:"
  echo "  ${ENGINE} run --rm ${LOCAL_PREFIX}${IMAGE_NAME}:dev"
  echo ""
  exit 0
fi

IMAGE_TAG="${1:-latest}"
# Always fully qualified: Podman names local images localhost/..., Docker would
# not. One name means an archive built by either engine runs under either
# compose file.
IMAGE_NAME="localhost/${IMAGE_NAME}"
FULL_TAG="${IMAGE_NAME}:${IMAGE_TAG}"
OUT_FILE="${OUT_DIR}/tenman-bot-${IMAGE_TAG}.tar"

# --target runtime is required: `runtime` is not the last stage in the
# Containerfile, so an untargeted build would produce the dev image.
echo "==> Building image: ${FULL_TAG} (target: runtime)"
# -f: Docker only looks for "Dockerfile" by default; ours is Containerfile.
# A versioned build is also tagged latest, which is what the compose files run,
# unless TAG_LATEST=0 (test builds that must not replace a running deployment's image).
LATEST_ARGS=()
if [[ "${TAG_LATEST:-1}" != "0" ]]; then
  LATEST_ARGS=(-t "${IMAGE_NAME}:latest")
fi
"${ENGINE}" build "${LOAD_FLAG[@]}" --platform linux/amd64 -f "${HERE}/Containerfile" --target runtime \
  -t "${FULL_TAG}" "${LATEST_ARGS[@]}" "${HERE}"

echo "==> Exporting to: ${OUT_FILE}"
mkdir -p "${OUT_DIR}"
# podman save refuses to write into an existing docker-archive
# ("docker-archive doesn't support modifying existing images"), so clear it first.
# Harmless for docker save, which overwrites.
rm -f "${OUT_FILE}"
# Both tags go in the archive, so `load` on the server yields :latest directly.
if [[ "${IMAGE_TAG}" != "latest" && "${TAG_LATEST:-1}" != "0" ]]; then
  if [[ "${ENGINE}" == "podman" ]]; then
    "${ENGINE}" save --multi-image-archive -o "${OUT_FILE}" "${FULL_TAG}" "${IMAGE_NAME}:latest"
  else
    "${ENGINE}" save -o "${OUT_FILE}" "${FULL_TAG}" "${IMAGE_NAME}:latest"
  fi
else
  "${ENGINE}" save -o "${OUT_FILE}" "${FULL_TAG}"
fi

SIZE=$(du -sh "${OUT_FILE}" | cut -f1)
echo "==> Done — ${OUT_FILE} (${SIZE})"
echo ""
echo "Deploy to the VPS (works with either engine there):"
echo "  scp ${OUT_FILE} compose.yaml compose.docker.yaml user@your-vps:~/tenman-bot/"
echo "  podman:  ssh user@your-vps 'cd ~/tenman-bot && podman load -i $(basename "${OUT_FILE}") && podman compose up -d'"
echo "  docker:  ssh user@your-vps 'cd ~/tenman-bot && docker load -i $(basename "${OUT_FILE}") && docker compose -f compose.docker.yaml up -d'"
echo ""
echo "The archive carries ${FULL_TAG} and ${IMAGE_NAME}:latest; both compose files run :latest."
