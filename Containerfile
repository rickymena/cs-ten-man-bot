# Multi-stage build.
#
#   builder  compiles dependency wheels; never shipped
#   runtime  the production image    -- podman build --target runtime
#   dev      runtime + the test suite -- podman build --target dev
#
# `runtime` is NOT the last stage, so always pass --target. build.sh does.

ARG PYTHON_BASE=docker.io/library/python:3.14-slim

# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS runtime
WORKDIR /app

# Install from the prebuilt wheels only: no compiler, no network resolution.
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
 && rm -rf /wheels

# Explicit allowlist, never `COPY . .`: a new file at the repo root (an .env,
# a database) cannot reach the image unless it is named here.
COPY bot.py healthcheck.py LICENSE NOTICE ./
COPY utils/__init__.py utils/alerts.py utils/config.py utils/database.py utils/db_queue.py \
     utils/backup.py utils/embeds.py utils/exit_codes.py utils/roster.py utils/sanitize.py utils/scheduler_state.py \
     utils/style.py utils/tracking.py utils/webhook.py utils/when.py ./utils/

# No .pyc writes: the root filesystem is read-only at runtime, under any uid.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/data/tenman.db \
    BACKUP_DIR=/app/backups \
    HEARTBEAT_PATH=/app/logs/heartbeat.json

# The bot's own service account: no shell, no home, no password, no sudo.
# Both compose files run as it; the host folders it writes (data/ logs/
# backups/) and its secrets must be owned by uid 10001 (see the guide).
# The code under /app stays root-owned and read-only to it.
RUN groupadd --gid 10001 tenman \
 && useradd --uid 10001 --gid 10001 --no-create-home --no-log-init --home-dir /nonexistent \
            --shell /usr/sbin/nologin tenman
USER 10001:10001

# Run under an init (compose `init: true`) so SIGTERM reaches the bot.
CMD ["python", "bot.py"]

# ---------------------------------------------------------------------------
FROM runtime AS dev
# The test image installs packages and writes caches: root inside a throwaway container.
USER root
COPY requirements-dev.txt pytest.ini ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY devtool.py ./
COPY tests/ ./tests/
CMD ["pytest", "-q"]
