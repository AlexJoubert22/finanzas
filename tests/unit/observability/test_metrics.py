"""Tests for the Prometheus metrics registry + /metrics endpoint (FASE 13.1)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from mib.api.app import create_app
from mib.observability.metrics import (
    _reset_for_tests,
    get_metrics_registry,
    render_metrics_text,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    """Each test gets a fresh registry — no leak across tests."""
    _reset_for_tests()


# ─── Registry shape ──────────────────────────────────────────────────


def test_registry_has_all_12_core_metrics() -> None:
    """Spec lock-in: 12 named metrics, none missing."""
    get_metrics_registry()  # warm the singleton
    expected_names = {
        "mib_signals_generated_total",
        "mib_orders_placed_total",
        "mib_reconcile_discrepancies_found_total",
        "mib_critical_incident_total",
        "mib_pnl_realized_eur",
        "mib_pnl_unrealized_eur",
        "mib_drawdown_pct",
        "mib_active_positions",
        "mib_days_clean_streak",
        "mib_circuit_breaker_state",
        "mib_ai_provider_quota_used_pct",
        "mib_api_latency_seconds",
    }
    text = render_metrics_text().decode()
    for name in expected_names:
        assert name in text, f"missing metric: {name}"


def test_registry_singleton_returns_same_instance() -> None:
    a = get_metrics_registry()
    b = get_metrics_registry()
    assert a is b


def test_signals_counter_tracks_increments() -> None:
    reg = get_metrics_registry()
    reg.signals_generated_total.labels(
        strategy_id="scanner.oversold.v1", status="pending"
    ).inc(3)
    text = render_metrics_text().decode()
    assert (
        'mib_signals_generated_total{status="pending",'
        'strategy_id="scanner.oversold.v1"} 3.0'
    ) in text


def test_pnl_gauge_set_value() -> None:
    reg = get_metrics_registry()
    reg.pnl_realized_eur.set(123.45)
    text = render_metrics_text().decode()
    assert "mib_pnl_realized_eur 123.45" in text


def test_api_latency_histogram_observes() -> None:
    reg = get_metrics_registry()
    reg.api_latency_seconds.labels(
        exchange="binance", endpoint="fetch_balance"
    ).observe(0.42)
    text = render_metrics_text().decode()
    assert "mib_api_latency_seconds_bucket" in text
    assert 'exchange="binance"' in text
    assert 'endpoint="fetch_balance"' in text


def test_circuit_breaker_state_gauge_per_label() -> None:
    reg = get_metrics_registry()
    reg.circuit_breaker_state.labels(name="binance_orders").set(2)
    text = render_metrics_text().decode()
    assert 'mib_circuit_breaker_state{name="binance_orders"} 2.0' in text


# ─── Endpoint ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_text() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # Spot-check 3 metrics — full set already verified in registry test.
    assert "mib_signals_generated_total" in body
    assert "mib_pnl_realized_eur" in body
    assert "mib_critical_incident_total" in body
