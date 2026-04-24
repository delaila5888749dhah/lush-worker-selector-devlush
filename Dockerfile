# syntax=docker/dockerfile:1.7
#
# Lush Givex worker — reproducible container (issue #226).
#
# Base: python:3.11-slim (matches CI python-version).
# Runtime:
#   - Chromium + driver pre-installed for selenium-wire/CDP flows.
#   - Non-root user `worker` owns /app.
#   - HEALTHCHECK hits the orchestrator health endpoint on :8080.
#
# Build:
#     docker build -t lush-worker:latest .
# Run:
#     docker run --rm --env-file .env -p 8080:8080 lush-worker:latest
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System dependencies: Chromium + driver + CA certs + curl (healthcheck).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        ca-certificates \
        curl \
        tini \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the pinned lockfile so the image is
# reproducible and hash-verified.
WORKDIR /app
COPY requirements.txt requirements-lock.txt ./
RUN pip install --require-hashes -r requirements-lock.txt

# Copy application sources.  `.dockerignore` strips dev-only paths.
COPY . .

# Non-root user.
RUN useradd --create-home --shell /bin/bash worker \
 && chown -R worker:worker /app
USER worker

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app"]
