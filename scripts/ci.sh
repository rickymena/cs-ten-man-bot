#!/usr/bin/env bash
# Run the same checks as .github/workflows/ci.yml locally.
#   scripts/ci.sh           lint + tests + audit
#   scripts/ci.sh --image   also build the images and test inside them
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}

echo "==> lint";   "$PY" -m ruff check .
echo "==> unit";   "$PY" -m pytest -q -m "not sim"
echo "==> sim";    "$PY" -m pytest -q -m sim
echo "==> audit";  "$PY" -m pip_audit -r requirements.txt --strict

if [[ "${1:-}" == "--image" ]]; then
  ENGINE=${CONTAINER_ENGINE:-podman}
  # Separate tags: never touch localhost/tenman-bot:latest, which a local deployment may be running.
  echo "==> image (dev)";     "$ENGINE" build -q -f Containerfile --target dev -t localhost/tenman-bot:dev . >/dev/null
  "$ENGINE" run --rm localhost/tenman-bot:dev
  echo "==> image (runtime)"; "$ENGINE" build -q -f Containerfile --target runtime -t localhost/tenman-bot:ci-check . >/dev/null
  set +e; "$ENGINE" run --rm localhost/tenman-bot:ci-check >/dev/null 2>&1; code=$?; set -e
  [[ $code -eq 7 ]] || { echo "runtime smoke test: expected exit 7 without secrets, got $code"; exit 1; }
fi
echo "==> all checks passed"
