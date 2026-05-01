"""Pre-flight checklist (FASE 14.1).

Snapshots every readiness check the operator should pass before
flipping the master kill switch via :func:`/go_live`. Each check
returns a :class:`CheckResult` with three levels of severity:

- ``critical``: blocks the report's ``ready=True``.
- ``warning``: flagged but does not block (informational).
- ``info``: passing-only signal.

The /preflight command renders the report; /go_live's first step
calls :func:`run_preflight` and refuses to send the 2FA code unless
``ready=True``.

The function is **read-mostly**: every check is a read, never a
write. Failures don't crash; a check that can't run returns a
critical failure with the exception in ``details``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.config import Settings, get_settings
from mib.db.models import ReconcileRunRow
from mib.db.session import async_session_factory
from mib.observability.clean_streak import compute_days_clean_streak
from mib.observability.scheduler_health import get_scheduler_health
from mib.trading.mode import TradingMode
from mib.trading.mode_guards import (
    closed_trades_in_mode,
    days_in_current_mode,
)
from mib.trading.risk.state import TradingStateService

#: Severity literals (string for JSON-friendly serialisation).
CheckSeverity = Literal["info", "warning", "critical"]

#: Days clean-streak required before /go_live can succeed.
MIN_CLEAN_STREAK_FOR_LIVE: int = 60

#: Minimum days the bot must have been in PAPER mode + minimum trade
#: count there before LIVE is "earned".
MIN_DAYS_IN_PAPER: int = 30
MIN_TRADES_IN_PAPER: int = 50

#: Reconcile rows older than this make the "reconcile clean" check
#: stale (treated as critical — a reconcile that hasn't run in 24h is
#: a sign the safety net is asleep).
RECONCILE_MAX_STALE_HOURS: int = 24


@dataclass(frozen=True)
class CheckResult:
    """One pre-flight verdict."""

    name: str
    passed: bool
    details: str
    severity: CheckSeverity = "critical"


@dataclass(frozen=True)
class PreflightReport:
    """Outcome of one preflight pass."""

    ran_at: datetime
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """``True`` iff every ``critical`` check passed."""
        return not any(
            c.severity == "critical" and not c.passed for c in self.checks
        )

    @property
    def failed_critical(self) -> list[CheckResult]:
        return [c for c in self.checks if c.severity == "critical" and not c.passed]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.severity == "warning" and not c.passed]


# ─── Public API ──────────────────────────────────────────────────────


async def run_preflight(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
) -> PreflightReport:
    """Run every readiness check. Pure-read, never crashes."""
    sf = session_factory or async_session_factory
    s = settings or get_settings()

    checks: list[CheckResult] = []
    checks.append(await _check_trading_state(sf=sf))
    checks.append(_check_gates_registered())
    checks.append(_check_scheduler_alive())
    checks.append(await _check_reconcile_clean(sf=sf))
    checks.append(await _check_clean_streak(sf=sf))
    checks.append(await _check_paper_validation(sf=sf))
    checks.append(_check_api_keys(s=s))
    checks.append(_check_capital_bracket(s=s))
    checks.append(_check_backups_recent())
    checks.append(_check_dead_man_configured(s=s))

    return PreflightReport(
        ran_at=datetime.now(UTC).replace(tzinfo=None),
        checks=checks,
    )


def format_preflight_html(report: PreflightReport) -> str:
    """Render the report as a Telegram HTML message."""
    from mib.telegram.formatters import esc  # noqa: PLC0415

    lines = ["🔍 <b>Pre-flight checklist</b>"]
    for c in report.checks:
        icon = "✅" if c.passed else ("⚠️" if c.severity == "warning" else "❌")
        lines.append(
            f"  {icon} <b>{esc(c.name)}</b> · "
            f"<i>{esc(c.severity)}</i>\n"
            f"     <code>{esc(c.details[:240])}</code>"
        )
    lines.append("")
    if report.ready:
        lines.append("✅ <b>READY</b> for LIVE — all critical checks passed.")
        if report.warnings:
            lines.append(
                f"<i>(but {len(report.warnings)} warning(s) — review)</i>"
            )
    else:
        lines.append(
            f"❌ <b>NOT READY</b>. {len(report.failed_critical)} critical "
            "check(s) failed."
        )
    return "\n".join(lines)


# ─── Individual checks ──────────────────────────────────────────────


async def _check_trading_state(
    *, sf: async_sessionmaker[AsyncSession]
) -> CheckResult:
    """Singleton row exists and ``enabled=False`` (paradoxical but
    correct — a /preflight pre-LIVE expects the kill switch to STILL
    be off; flipping it is the operator's explicit /go_live action).
    """
    try:
        state = await TradingStateService(sf).get()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="trading_state",
            passed=False,
            details=f"failed to read singleton: {exc}",
            severity="critical",
        )
    if state.enabled:
        return CheckResult(
            name="trading_state",
            passed=False,
            details=(
                "trading_state.enabled is already True — /preflight is for "
                "pre-LIVE; bot is already live, use /risk and /panic instead"
            ),
            severity="warning",
        )
    return CheckResult(
        name="trading_state",
        passed=True,
        details=f"singleton present, enabled=False, mode={state.mode}",
        severity="critical",
    )


def _check_gates_registered() -> CheckResult:
    """All 6 base risk gates plus the optional MinAIConfidenceGate."""
    expected = {
        "kill_switch", "daily_drawdown", "exposure_per_ticker",
        "correlation_group", "max_concurrent_trades",
        "signals_per_hour",
    }
    try:
        from mib.api.dependencies import get_risk_manager  # noqa: PLC0415

        manager = get_risk_manager()
        names = {g.name for g in manager._gates}  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="risk_gates",
            passed=False,
            details=f"failed to inspect risk_manager: {exc}",
            severity="critical",
        )
    missing = expected - names
    if missing:
        return CheckResult(
            name="risk_gates",
            passed=False,
            details=f"missing gates: {sorted(missing)}",
            severity="critical",
        )
    return CheckResult(
        name="risk_gates",
        passed=True,
        details=f"{len(names)} gates registered: {sorted(names)}",
        severity="critical",
    )


def _check_scheduler_alive() -> CheckResult:
    """Scheduler has ticked recently — proxy for 'running' since the
    APScheduler instance is private to the lifespan.
    """
    health = get_scheduler_health()
    if health.last_tick_at is None:
        return CheckResult(
            name="scheduler",
            passed=False,
            details="scheduler has never ticked — boot the bot first",
            severity="critical",
        )
    age = (
        datetime.now(UTC).replace(tzinfo=None) - health.last_tick_at
    ).total_seconds()
    if age > 90:
        return CheckResult(
            name="scheduler",
            passed=False,
            details=f"last tick was {int(age)}s ago — scheduler stalled",
            severity="critical",
        )
    return CheckResult(
        name="scheduler",
        passed=True,
        details=f"last tick {int(age)}s ago",
        severity="critical",
    )


async def _check_reconcile_clean(
    *, sf: async_sessionmaker[AsyncSession]
) -> CheckResult:
    """Last reconcile row was successful and < 24h old."""
    try:
        async with sf() as session:
            stmt = (
                select(ReconcileRunRow)
                .order_by(ReconcileRunRow.started_at.desc())
                .limit(1)
            )
            row = (await session.scalars(stmt)).first()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="reconcile_clean",
            passed=False,
            details=f"reconcile_runs query failed: {exc}",
            severity="critical",
        )
    if row is None:
        return CheckResult(
            name="reconcile_clean",
            passed=False,
            details="no reconcile_runs rows yet — wait for first scheduled run",
            severity="critical",
        )
    age_hours = (
        datetime.now(UTC).replace(tzinfo=None) - row.started_at
    ).total_seconds() / 3600
    if age_hours > RECONCILE_MAX_STALE_HOURS:
        return CheckResult(
            name="reconcile_clean",
            passed=False,
            details=(
                f"last reconcile {age_hours:.1f}h ago "
                f"(>{RECONCILE_MAX_STALE_HOURS}h) — stale"
            ),
            severity="critical",
        )
    if row.status == "error":
        return CheckResult(
            name="reconcile_clean",
            passed=False,
            details=f"last reconcile errored: {row.error_message}",
            severity="critical",
        )
    return CheckResult(
        name="reconcile_clean",
        passed=True,
        details=(
            f"last run {age_hours:.1f}h ago, status={row.status}, "
            f"discrepancies={row.orphan_exchange_count + row.orphan_db_count + row.balance_drift_count}"
        ),
        severity="critical",
    )


async def _check_clean_streak(
    *, sf: async_sessionmaker[AsyncSession]
) -> CheckResult:
    streak = await compute_days_clean_streak(session_factory=sf)
    passed = streak >= MIN_CLEAN_STREAK_FOR_LIVE
    return CheckResult(
        name="days_clean_streak",
        passed=passed,
        details=f"{streak}d (need >= {MIN_CLEAN_STREAK_FOR_LIVE}d)",
        severity="critical",
    )


async def _check_paper_validation(
    *, sf: async_sessionmaker[AsyncSession]
) -> CheckResult:
    """At least 30 days in PAPER mode + at least 50 closed trades there."""
    days = await days_in_current_mode(TradingMode.PAPER, sf)
    closed = await closed_trades_in_mode(TradingMode.PAPER, sf)
    passed = days >= MIN_DAYS_IN_PAPER and closed >= MIN_TRADES_IN_PAPER
    return CheckResult(
        name="paper_validation",
        passed=passed,
        details=(
            f"{days}d in PAPER (need >= {MIN_DAYS_IN_PAPER}), "
            f"{closed} trades closed (need >= {MIN_TRADES_IN_PAPER})"
        ),
        severity="critical",
    )


def _check_api_keys(*, s: Settings) -> CheckResult:
    """Sandbox keys present (we test against testnet first; LIVE keys
    arrive at /go_live time and are validated by the executor).
    """
    if not s.binance_sandbox_api_key or not s.binance_sandbox_secret:
        return CheckResult(
            name="api_keys",
            passed=False,
            details="BINANCE_SANDBOX_API_KEY/SECRET missing in .env",
            severity="critical",
        )
    return CheckResult(
        name="api_keys",
        passed=True,
        details="sandbox keys configured (LIVE keys validated at /go_live)",
        severity="critical",
    )


def _check_capital_bracket(*, s: Settings) -> CheckResult:
    """Placeholder: TIER_1 cap = 200€. Live equity check lands once
    the first /go_live call wires real LIVE keys.
    """
    _ = s  # capital_bracket setting not yet introduced; placeholder severity=warning
    return CheckResult(
        name="capital_bracket",
        passed=True,
        details="TIER_1 default (≤200€); live verification at /go_live",
        severity="warning",
    )


def _check_backups_recent() -> CheckResult:
    """FASE 26 will implement real backup verification."""
    return CheckResult(
        name="backups_recent",
        passed=True,
        details="backup_check_pending_phase_26",
        severity="warning",
    )


def _check_dead_man_configured(*, s: Settings) -> CheckResult:
    """The dead-man heartbeat token must be set so an external monitor
    can ping the endpoint.
    """
    if not s.heartbeat_token:
        return CheckResult(
            name="dead_man",
            passed=False,
            details=(
                "HEARTBEAT_TOKEN empty — dead-man heartbeat disabled. "
                "See docs/DEAD-MAN-SETUP.md"
            ),
            severity="critical",
        )
    return CheckResult(
        name="dead_man",
        passed=True,
        details="HEARTBEAT_TOKEN configured",
        severity="critical",
    )


# Suppress the unused-import warning for asyncio (kept for future
# concurrent-check optimisation).
_unused_asyncio = asyncio
_unused_timedelta = timedelta
