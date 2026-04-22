# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────
# Market Intelligence Bot · multi-stage Dockerfile
# Target: final image < 200 MB (spec §11).
# ─────────────────────────────────────────────────────────────────────────

########################
# 1. BUILDER
########################
FROM python:3.12-slim AS builder

# uv binary comes from its official image — small, pinned, no wget.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

# System libs that some wheels (pandas/numpy, mplfinance, aiohttp…) still
# need at *build* time even when they publish binary wheels for 3.12.
RUN apt-get update -q \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy lockfile + project metadata first so the layer is cached when only
# source code changes.
COPY pyproject.toml uv.lock README.md ./

# Resolve runtime deps (no dev group).
ENV UV_PYTHON=python3.12 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --no-dev --no-install-project

# Now the project itself (editable install produces the mib wheel).
COPY src ./src
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

########################
# 2. RUNTIME
########################
FROM python:3.12-slim AS runtime

# Non-root user (spec §13).
RUN groupadd --system --gid 1001 mib \
    && useradd  --system --uid 1001 --gid 1001 --home /app --shell /bin/false mib

# Minimal runtime deps. `curl` is for the HEALTHCHECK.
RUN apt-get update -q \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only what runtime needs: the venv and the source tree.
COPY --from=builder --chown=mib:mib /app/.venv /app/.venv
COPY --from=builder --chown=mib:mib /app/src /app/src
COPY --from=builder --chown=mib:mib /app/alembic.ini /app/alembic.ini

# Data volume target. Mounted by compose; chown so the non-root user writes.
RUN mkdir -p /app/data && chown -R mib:mib /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    # Spec §11bis — reduces glibc memory fragmentation on long-running workers.
    MALLOC_ARENA_MAX=2

EXPOSE 8000

USER mib

# tini reaps zombie children cleanly (helpful for future APScheduler threads).
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "-m", "mib.main"]
