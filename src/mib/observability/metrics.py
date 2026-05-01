"""Prometheus metrics registry (FASE 13.1).

Exposes 12 core metrics named ``mib_<dominio>_<métrica>_<unidad>``.
The naming scheme matches the FASE 13 spec so downstream Grafana
dashboards stay portable across environments.

The registry is a singleton: every emitter imports
:func:`get_metrics_registry` and bumps the metric directly. The
FastAPI router renders the text exposition via
:func:`render_metrics_text` on ``GET /metrics``.

We do NOT use the default global registry — a fresh
``CollectorRegistry`` keeps test suites hermetic (pytest doesn't
leak counter values across tests).
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest


@dataclass
class MetricsRegistry:
    """Holder for every metric the bot exports.

    Constructed once via :func:`get_metrics_registry`; emitters keep
    a reference and bump fields directly. Reset by calling
    :meth:`reset` (test-only — production never resets).
    """

    registry: CollectorRegistry

    # ─── Counters ─────────────────────────────────────────────────
    signals_generated_total: Counter
    """Per (strategy_id, status). Emitted by SignalRepository on
    every persist or transition."""

    orders_placed_total: Counter
    """Per (exchange, status). Emitted by CCXTTrader.create_order
    on every exchange call regardless of outcome."""

    reconcile_discrepancies_found_total: Counter
    """Per (kind). Emitted by Reconciler at the end of every run.
    kinds: orphan_exchange | orphan_db | balance_drift."""

    critical_incident_total: Counter
    """Per (type). Bumped by every emit_incident() call."""

    # ─── Gauges (current state) ───────────────────────────────────
    pnl_realized_eur: Gauge
    pnl_unrealized_eur: Gauge
    drawdown_pct: Gauge
    active_positions: Gauge
    days_clean_streak: Gauge
    """Set every time the metric is read; updated by the heartbeat
    job and the /clean_streak Telegram command."""

    circuit_breaker_state: Gauge
    """Per (name). Values: 0=closed (healthy), 1=half (testing),
    2=open (tripped)."""

    ai_provider_quota_used_pct: Gauge
    """Per (provider). 0..100. Refreshed by the AI router's usage
    tracker."""

    # ─── Histograms ────────────────────────────────────────────────
    api_latency_seconds: Histogram
    """Per (exchange, endpoint). Buckets: 0.05, 0.1, 0.25, 0.5, 1,
    2.5, 5, 10 — chosen to span healthy CCXT calls (50-500ms) up to
    timeouts."""


# ─── Module singleton ───────────────────────────────────────────────


_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    """Lazy singleton. Tests can wipe via :func:`_reset_for_tests`."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = _build_registry()
    return _registry


def render_metrics_text() -> bytes:
    """Render the registry to Prometheus text format.

    Returned bytes are streamed verbatim by ``GET /metrics`` with
    Content-Type ``text/plain; version=0.0.4; charset=utf-8`` (the
    default the prometheus_client client picks).
    """
    reg = get_metrics_registry()
    return generate_latest(reg.registry)


# ─── Internal ───────────────────────────────────────────────────────


def _build_registry() -> MetricsRegistry:
    reg = CollectorRegistry()
    return MetricsRegistry(
        registry=reg,
        signals_generated_total=Counter(
            "mib_signals_generated_total",
            "Total trading signals persisted, partitioned by strategy and status.",
            labelnames=("strategy_id", "status"),
            registry=reg,
        ),
        orders_placed_total=Counter(
            "mib_orders_placed_total",
            "Total orders placed against an exchange.",
            labelnames=("exchange", "status"),
            registry=reg,
        ),
        reconcile_discrepancies_found_total=Counter(
            "mib_reconcile_discrepancies_found_total",
            "Discrepancies detected by the reconciler, partitioned by kind.",
            labelnames=("kind",),
            registry=reg,
        ),
        critical_incident_total=Counter(
            "mib_critical_incident_total",
            "Critical incidents emitted, partitioned by type.",
            labelnames=("type",),
            registry=reg,
        ),
        pnl_realized_eur=Gauge(
            "mib_pnl_realized_eur",
            "Cumulative realised PnL in EUR (quote currency).",
            registry=reg,
        ),
        pnl_unrealized_eur=Gauge(
            "mib_pnl_unrealized_eur",
            "Open-position unrealised PnL in EUR.",
            registry=reg,
        ),
        drawdown_pct=Gauge(
            "mib_drawdown_pct",
            "Current drawdown as a fraction of peak equity (0..1).",
            registry=reg,
        ),
        active_positions=Gauge(
            "mib_active_positions",
            "Count of trades currently in 'open' status.",
            registry=reg,
        ),
        days_clean_streak=Gauge(
            "mib_days_clean_streak",
            "Days since the last reset of the clean-incident streak.",
            registry=reg,
        ),
        circuit_breaker_state=Gauge(
            "mib_circuit_breaker_state",
            "Circuit breaker state: 0=closed, 1=half-open, 2=open.",
            labelnames=("name",),
            registry=reg,
        ),
        ai_provider_quota_used_pct=Gauge(
            "mib_ai_provider_quota_used_pct",
            "Fraction of the daily AI provider quota consumed (0..100).",
            labelnames=("provider",),
            registry=reg,
        ),
        api_latency_seconds=Histogram(
            "mib_api_latency_seconds",
            "Latency of upstream API calls (exchange / data sources).",
            labelnames=("exchange", "endpoint"),
            buckets=(
                0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
            ),
            registry=reg,
        ),
    )


def _reset_for_tests() -> None:
    """Test-only helper: clear the singleton so each test starts with
    a clean :class:`CollectorRegistry`. Production NEVER calls this.
    """
    global _registry  # noqa: PLW0603
    _registry = None
