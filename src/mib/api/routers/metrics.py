"""``GET /metrics`` — Prometheus text exposition (FASE 13.1).

Plain-text endpoint matching Prometheus' scrape contract. Content
type ``text/plain; version=0.0.4; charset=utf-8`` is the default the
client library produces; we forward it verbatim.

Endpoint is loopback-only by virtue of the FastAPI bind address
(127.0.0.1 in production) — Prometheus must scrape via SSH tunnel
or sidecar. No auth is enforced here because exposure is gated at
the network layer; if that ever changes, add a token check here.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from mib.observability.metrics import render_metrics_text

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Return the latest registry snapshot in Prometheus text format."""
    body = render_metrics_text()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
