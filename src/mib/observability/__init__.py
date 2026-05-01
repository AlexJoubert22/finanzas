"""Observability primitives (FASE 13).

Prometheus metrics, critical-incidents log, days-clean-streak
computation, /panic + /heartbeat + 6h Telegram heartbeat all live
inside this subpackage. Sub-commits build it in this order:

- 13.1 — Prometheus /metrics + 12 core metrics
- 13.2 — critical_incidents table + 7-type enum
- 13.3 — auto-detection wiring (6 emitters)
- 13.4 — /incident manual command
- 13.5 — days_clean_streak() + wire SEMI_AUTO->LIVE guard
- 13.6 — /panic
- 13.7 — /heartbeat public + DEAD-MAN-SETUP runbook
- 13.8 — 6h Telegram heartbeat
"""

from mib.observability.metrics import (
    MetricsRegistry,
    get_metrics_registry,
    render_metrics_text,
)

__all__ = [
    "MetricsRegistry",
    "get_metrics_registry",
    "render_metrics_text",
]
