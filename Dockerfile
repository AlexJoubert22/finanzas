# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────
# Market Intelligence Bot · multi-stage Dockerfile
# Target: final image as small as reasonable with pandas/numpy/mplfinance
# (spec §11 aim <200 MB is aspirational with this stack; realistic <900 MB).
# ─────────────────────────────────────────────────────────────────────────

########################
# 1. BUILDER
########################
FROM python:3.12-slim AS builder

# uv binary comes from its official image — small, pinned, no wget.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

# System libs some wheels still need at build time.
RUN apt-get update -q \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build-time cache controls (condition F2.10.b of phase 2).
ENV UV_PYTHON=python3.12 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    PIP_NO_CACHE_DIR=1

# Copy lockfile + project metadata first so the layer is cached when only
# source code changes.
COPY pyproject.toml uv.lock README.md ./

# Resolve runtime deps (no dev group).
RUN uv sync --frozen --no-dev --no-install-project

# Now the project itself (editable install produces the mib wheel).
COPY src ./src
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# Trim fat from the installed venv (condition F2.10.c):
#   - tests/ folders inside every installed package (often 5-30 MB each)
#   - *.pyc orphans (rebuilt on first run; cheap to drop)
#   - *.dist-info/RECORD (used only by uninstall, not at runtime)
#   - __pycache__ dirs in site-packages (UV_COMPILE_BYTECODE regenerates)
# This takes seconds and shaves 150-300 MB in practice.
RUN set -eux; \
    find .venv -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true; \
    find .venv -type d -name 'test' -prune -exec rm -rf {} + 2>/dev/null || true; \
    find .venv -type f -name '*.pyc' -delete 2>/dev/null || true; \
    find .venv -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true; \
    find .venv -type f -name 'RECORD' -path '*dist-info*' -delete 2>/dev/null || true

########################
# 2. RUNTIME
########################
FROM python:3.12-slim AS runtime

# Non-root user (spec §13). Home is /app so PYTHONPATH resolution and any
# future cache files fall inside the volume-mounted area.
RUN groupadd --system --gid 1001 mib \
    && useradd  --system --uid 1001 --gid 1001 --home /app --shell /bin/false mib

# Minimal runtime deps: curl for HEALTHCHECK, tini as PID 1.
RUN apt-get update -q \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --chown applied directly on COPY; avoids the expensive `chown -R`
# (condition F2.10.a of phase 2: previous builds were spending 25 min
# changing ownership on ~270k venv files).
COPY --from=builder --chown=mib:mib /app/.venv    /app/.venv
COPY --from=builder --chown=mib:mib /app/src      /app/src
COPY --from=builder --chown=mib:mib /app/alembic.ini /app/alembic.ini
COPY --chown=mib:mib scripts/docker-entrypoint.sh /app/docker-entrypoint.sh

# Data volume target, writable by the non-root user.
RUN install -d -o mib -g mib /app/data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    # Spec §11bis — reduces glibc memory fragmentation on long-running workers.
    MALLOC_ARENA_MAX=2

EXPOSE 8000

USER mib

# tini reaps zombie children cleanly (helpful for APScheduler threads later).
# The shell script applies Alembic migrations idempotently then execs uvicorn.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1
