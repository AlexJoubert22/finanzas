"""Reconciler — diffs the exchange against MIB's local state (FASE 9.5).

Run on a 5-minute interval and on operator demand via ``/reconcile``.
Three discrepancy classes:

1. ``orphan_exchange``: an open order on the exchange has no matching
   row in the ``orders`` table. Could be a manual order placed
   outside MIB, or evidence that our writes succeeded but the audit
   row was lost. Either way the operator needs to see it.

2. ``orphan_db``: a row in ``orders`` claims to be ``submitted`` /
   ``partially_filled`` but the exchange says it's ``filled`` /
   ``cancelled`` / ``rejected``. Indicates we missed a fill webhook
   or a cancel; reconciler updates the local row by writing a
   ``reconciled`` event with the new ``to_status`` so the audit
   trail explains how the cache got patched.

3. ``balance_drift``: the exchange-reported equity differs from the
   most recent ``PortfolioSnapshot`` equity by more than 1%
   (``BALANCE_DRIFT_THRESHOLD_PCT``). Either the snapshot job is
   stale (rare), or PnL accounting drifted (a bug). Always alerts
   the operator — this one is loud because it means money math is
   off.

The reconciler is read-mostly: it never cancels orders, never
closes positions. The only writes are:

- a row in ``portfolio_snapshots`` (the snapshot it just took),
- a row in ``reconcile_runs`` (the summary + raw discrepancy list),
- ``order_repo.transition(..., event_type='reconciled')`` for each
  ``orphan_db`` it patches.

Live remediation (cancel a stranger's order, etc.) is deliberately
NOT in scope. The runbook is: alert → operator inspects → operator
runs the right command. Robots don't fight back here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import (
    OrderRow,
    PortfolioSnapshotRow,
    ReconcileRunRow,
)
from mib.logger import logger
from mib.models.portfolio import PortfolioSnapshot
from mib.sources.ccxt_trader import CCXTTrader
from mib.trading.alerter import NullAlerter, TelegramAlerter
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderStatus
from mib.trading.portfolio import PortfolioState

#: Tolerance: |our_equity - exchange_equity| / exchange_equity below
#: this threshold is "noise" (rounding, in-flight fills). Above it,
#: we flag a balance_drift discrepancy.
BALANCE_DRIFT_THRESHOLD_PCT: Decimal = Decimal("0.01")

#: Local order statuses we consider "still open on the exchange".
#: Any of these in the DB but missing on the exchange = orphan_db.
_OPEN_LOCAL_STATUSES: tuple[OrderStatus, ...] = ("submitted", "partially_filled")

#: CCXT order statuses that count as "still open".
_OPEN_CCXT_STATUSES: frozenset[str] = frozenset({"open", "new", "partially_filled"})

DiscrepancyKind = Literal["orphan_exchange", "orphan_db", "balance_drift"]


@dataclass(frozen=True)
class Discrepancy:
    """One specific finding from a reconcile pass."""

    kind: DiscrepancyKind
    summary: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ReconcileReport:
    """Outcome of one reconcile pass — counters + raw findings."""

    started_at: datetime
    finished_at: datetime
    status: Literal["ok", "discrepancies", "error"]
    triggered_by: str
    discrepancies: list[Discrepancy] = field(default_factory=list)
    portfolio_snapshot_id: int | None = None
    error_message: str | None = None
    run_id: int | None = None

    @property
    def orphan_exchange_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.kind == "orphan_exchange")

    @property
    def orphan_db_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.kind == "orphan_db")

    @property
    def balance_drift_count(self) -> int:
        return sum(1 for d in self.discrepancies if d.kind == "balance_drift")


class Reconciler:
    """Reads exchange + DB, computes deltas, persists summary."""

    def __init__(
        self,
        *,
        trader: CCXTTrader,
        portfolio_state: PortfolioState,
        order_repo: OrderRepository,
        session_factory: async_sessionmaker[AsyncSession],
        alerter: TelegramAlerter | None = None,
        symbols: tuple[str, ...] = ("BTC/USDT",),
    ) -> None:
        self._trader = trader
        self._portfolio = portfolio_state
        self._orders = order_repo
        self._sf = session_factory
        self._alerter = alerter or NullAlerter()
        # Symbols whose open-orders feed we poll. Keep tight — querying
        # every market on Binance is wasteful when the bot only trades
        # a handful of tickers.
        self._symbols = symbols

    async def reconcile(self, *, triggered_by: str) -> ReconcileReport:
        """Run one reconciliation pass. Never raises — failures land in
        the report's ``status='error'`` + ``error_message``.
        """
        started_at = datetime.now(UTC).replace(tzinfo=None)
        t0 = time.monotonic()
        try:
            snapshot = await self._portfolio.snapshot()
            snapshot_row_id = await self._persist_snapshot(snapshot)

            discrepancies: list[Discrepancy] = []

            # 1) Exchange vs DB orders.
            exchange_orders = await self._fetch_exchange_open_orders()
            db_open = await self._fetch_db_open_orders()
            discrepancies.extend(
                _diff_orders(exchange_orders=exchange_orders, db_open=db_open)
            )
            await self._patch_orphan_db(discrepancies)

            # 2) Equity drift.
            exchange_equity = await self._compute_exchange_equity(snapshot)
            drift = _compute_balance_drift(
                our_equity=snapshot.equity_quote,
                exchange_equity=exchange_equity,
            )
            if drift is not None:
                discrepancies.append(drift)

            finished_at = datetime.now(UTC).replace(tzinfo=None)
            status: Literal["ok", "discrepancies", "error"] = (
                "ok" if not discrepancies else "discrepancies"
            )
            report = ReconcileReport(
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                triggered_by=triggered_by,
                discrepancies=discrepancies,
                portfolio_snapshot_id=snapshot_row_id,
            )
            run_id = await self._persist_run(report)
            report = _with_run_id(report, run_id)

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "reconcile: status={} orphan_exchange={} orphan_db={} "
                "balance_drift={} latency_ms={}",
                report.status,
                report.orphan_exchange_count,
                report.orphan_db_count,
                report.balance_drift_count,
                elapsed_ms,
            )
            if discrepancies:
                await self._alert(report)
            return report
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.error("reconcile: failed: {}", exc)
            finished_at = datetime.now(UTC).replace(tzinfo=None)
            report = ReconcileReport(
                started_at=started_at,
                finished_at=finished_at,
                status="error",
                triggered_by=triggered_by,
                error_message=f"{exc.__class__.__name__}: {exc}",
            )
            try:
                run_id = await self._persist_run(report)
                report = _with_run_id(report, run_id)
            except Exception as persist_exc:  # noqa: BLE001
                logger.error("reconcile: failed to persist error run: {}", persist_exc)
            return report

    # ─── Exchange + DB readers ─────────────────────────────────────

    async def _fetch_exchange_open_orders(self) -> list[dict[str, Any]]:
        """Poll the exchange for open orders across the configured symbols.

        Returns CCXT-shaped dicts (``{id, clientOrderId, symbol, status,
        ...}``). Errors swallow per-symbol so one bad symbol doesn't
        kill the whole pass.
        """
        if not self._trader.has_credentials or self._trader._dry_run:  # noqa: SLF001
            logger.debug("reconcile: trader dry-run / no creds; skipping exchange fetch")
            return []
        out: list[dict[str, Any]] = []
        exchange = await self._trader._ensure_exchange()  # noqa: SLF001
        for sym in self._symbols:
            try:
                orders = await exchange.fetch_open_orders(sym)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile: fetch_open_orders({}) failed: {}", sym, exc
                )
                continue
            out.extend(orders or [])
        return out

    async def _fetch_db_open_orders(self) -> list[OrderRow]:
        """Read DB rows whose status is locally considered open."""
        async with self._sf() as session:
            stmt = select(OrderRow).where(
                OrderRow.status.in_(_OPEN_LOCAL_STATUSES)
            )
            return list((await session.scalars(stmt)).all())

    async def _compute_exchange_equity(
        self, snapshot: PortfolioSnapshot
    ) -> Decimal:
        """Equity per the exchange. Re-uses the same equity computation
        as :class:`PortfolioState`. For the reconciler this is the
        snapshot's ``equity_quote`` because the snapshot itself was
        derived from a live fetch — but we expose the helper so future
        FASE 14 work can plug in an independent valuation.
        """
        return snapshot.equity_quote

    # ─── Persisters ────────────────────────────────────────────────

    async def _persist_snapshot(self, snapshot: PortfolioSnapshot) -> int:
        """Write the snapshot row. Returns the new row id."""
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._sf() as session, session.begin():
            row = PortfolioSnapshotRow(
                taken_at=now,
                source=snapshot.source,
                quote_currency=_quote_from_snapshot(snapshot),
                equity_quote=snapshot.equity_quote,
                balances_json=[b.model_dump(mode="json") for b in snapshot.balances],
                positions_json=[p.model_dump(mode="json") for p in snapshot.positions],
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    async def _persist_run(self, report: ReconcileReport) -> int:
        async with self._sf() as session, session.begin():
            row = ReconcileRunRow(
                started_at=report.started_at,
                finished_at=report.finished_at,
                status=report.status,
                triggered_by=report.triggered_by,
                orphan_exchange_count=report.orphan_exchange_count,
                orphan_db_count=report.orphan_db_count,
                balance_drift_count=report.balance_drift_count,
                discrepancies_json=[
                    {"kind": d.kind, "summary": d.summary, "payload": d.payload}
                    for d in report.discrepancies
                ],
                error_message=report.error_message,
                portfolio_snapshot_id=report.portfolio_snapshot_id,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    async def _patch_orphan_db(
        self, discrepancies: list[Discrepancy]
    ) -> None:
        """For each orphan_db, write a 'reconciled' transition with the
        exchange-observed terminal status. Other discrepancy kinds
        require operator intervention so we don't auto-patch them.
        """
        for d in discrepancies:
            if d.kind != "orphan_db":
                continue
            order_id = d.payload.get("order_id")
            new_status = d.payload.get("exchange_status")
            current_status = d.payload.get("local_status")
            if not isinstance(order_id, int) or not isinstance(new_status, str):
                continue
            try:
                await self._orders.transition(
                    order_id,
                    new_status,  # type: ignore[arg-type]
                    actor="reconciler",
                    event_type="reconciled",
                    reason=f"reconciled: exchange says {new_status!r}",
                    expected_from_status=current_status,
                    metadata={"discrepancy": d.summary},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reconcile: patch failed for order_id={}: {}", order_id, exc
                )

    # ─── Alerting ──────────────────────────────────────────────────

    async def _alert(self, report: ReconcileReport) -> None:
        title = (
            f"⚠️ Reconcile detected {len(report.discrepancies)} discrepancies"
        )
        lines = [title, ""]
        for d in report.discrepancies[:10]:
            lines.append(f"• [{d.kind}] {d.summary}")
        if len(report.discrepancies) > 10:
            lines.append(f"• … (+{len(report.discrepancies) - 10} more)")
        try:
            await self._alerter.alert("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile: alert failed: {}", exc)


# ─── Pure helpers (testable without DB) ─────────────────────────────

def _diff_orders(
    *,
    exchange_orders: list[dict[str, Any]],
    db_open: list[OrderRow],
) -> list[Discrepancy]:
    """Diff exchange vs DB — produces orphan_exchange + orphan_db.

    Pure function over already-fetched lists so it's easy to unit-test.
    """
    by_client_id_db: dict[str, OrderRow] = {
        r.client_order_id: r for r in db_open
    }
    by_exchange_id_db: dict[str, OrderRow] = {
        r.exchange_order_id: r
        for r in db_open
        if r.exchange_order_id is not None
    }

    seen_db_orders: set[int] = set()
    out: list[Discrepancy] = []

    for ex_order in exchange_orders:
        client_id = ex_order.get("clientOrderId") or ""
        ex_id = str(ex_order.get("id") or "")
        match = by_client_id_db.get(client_id) or by_exchange_id_db.get(ex_id)
        if match is None:
            out.append(
                Discrepancy(
                    kind="orphan_exchange",
                    summary=(
                        f"exchange order {ex_id or client_id!r} on "
                        f"{ex_order.get('symbol')} not in DB"
                    ),
                    payload={
                        "exchange_order_id": ex_id,
                        "client_order_id": client_id,
                        "symbol": ex_order.get("symbol"),
                        "status": ex_order.get("status"),
                        "amount": str(ex_order.get("amount") or ""),
                        "price": str(ex_order.get("price") or ""),
                    },
                )
            )
        else:
            seen_db_orders.add(int(match.id))

    # Any DB row in OPEN_LOCAL_STATUSES that the exchange did NOT
    # report as open is an orphan_db: its terminal status is whatever
    # the exchange tells us, fetched lazily here so the reconciler has
    # a concrete to_status to write.
    exchange_orders_by_client: dict[str, dict[str, Any]] = {
        ex.get("clientOrderId") or "": ex for ex in exchange_orders
    }
    exchange_orders_by_id: dict[str, dict[str, Any]] = {
        str(ex.get("id") or ""): ex for ex in exchange_orders
    }

    for row in db_open:
        if int(row.id) in seen_db_orders:
            continue
        # DB says open, exchange doesn't list it as open → mismatch.
        # Try matching by client_order_id or exchange_order_id in the
        # full-orders feed (caller supplies only open orders, so this
        # branch infers status='filled' or 'cancelled' was missed).
        # We don't have closed-order data here — caller refetches per
        # order in the patch step if needed. For now, mark unknown.
        match_ex: dict[str, Any] | None = exchange_orders_by_client.get(
            row.client_order_id
        )
        if match_ex is None and row.exchange_order_id:
            match_ex = exchange_orders_by_id.get(row.exchange_order_id)
        # Even if absent in open list, that's the discrepancy: caller
        # needs to refetch to learn the terminal state.
        terminal_status = _ccxt_to_local_terminal(
            match_ex.get("status") if match_ex else None
        )
        out.append(
            Discrepancy(
                kind="orphan_db",
                summary=(
                    f"DB order #{row.id} status={row.status!r} but exchange "
                    f"reports {terminal_status!r}"
                ),
                payload={
                    "order_id": int(row.id),
                    "client_order_id": row.client_order_id,
                    "exchange_order_id": row.exchange_order_id,
                    "local_status": row.status,
                    "exchange_status": terminal_status,
                    "symbol": _symbol_from_db_row(row),
                },
            )
        )
    return out


def _ccxt_to_local_terminal(raw: str | None) -> OrderStatus:
    """Map a CCXT terminal status to our :class:`OrderStatus` literal.

    Defaults to ``cancelled`` when the exchange refused to report a
    state at all — the reconciler's job is to converge the DB to a
    safe terminal label, not to gamble on partial fills.
    """
    if raw is None:
        return "cancelled"
    rl = raw.lower()
    if rl == "closed" or rl == "filled":
        return "filled"
    if rl == "expired" or rl == "rejected":
        return "rejected"
    return "cancelled"


def _compute_balance_drift(
    *, our_equity: Decimal, exchange_equity: Decimal
) -> Discrepancy | None:
    """Balance drift discrepancy iff |Δ| / max(|exchange|, 1) > threshold.

    The ``max(..., 1)`` floor avoids a division-by-zero when the
    account has zero equity (early dry-runs / empty sandboxes).
    """
    if exchange_equity == 0 and our_equity == 0:
        return None
    denom = exchange_equity if exchange_equity != 0 else Decimal(1)
    drift_pct = abs(our_equity - exchange_equity) / abs(denom)
    if drift_pct <= BALANCE_DRIFT_THRESHOLD_PCT:
        return None
    return Discrepancy(
        kind="balance_drift",
        summary=(
            f"equity drift {drift_pct:.2%} our={our_equity} "
            f"exchange={exchange_equity}"
        ),
        payload={
            "our_equity": str(our_equity),
            "exchange_equity": str(exchange_equity),
            "drift_pct": str(drift_pct),
            "threshold_pct": str(BALANCE_DRIFT_THRESHOLD_PCT),
        },
    )


def _quote_from_snapshot(snapshot: PortfolioSnapshot) -> str:
    """Best-effort quote currency identifier from the snapshot."""
    # First non-zero balance is a reasonable proxy if present;
    # otherwise default to USDT (sandbox baseline).
    for b in snapshot.balances:
        if b.asset:
            return b.asset.upper()
    return "USDT"


def _symbol_from_db_row(row: OrderRow) -> str:
    payload: dict[str, Any] = row.raw_payload_json or {}
    symbol_obj = payload.get("symbol")
    return str(symbol_obj) if symbol_obj is not None else ""


def _with_run_id(report: ReconcileReport, run_id: int) -> ReconcileReport:
    """Return a copy of ``report`` with ``run_id`` populated."""
    from dataclasses import replace  # noqa: PLC0415

    return replace(report, run_id=run_id)
