"""SQLAlchemy ORM models — spec §7.

All tables are defined here so they are visible to Alembic's
autogenerate machinery via `target_metadata = Base.metadata`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mib.db.session import Base


# ─── Cache ────────────────────────────────────────────────────────────
class Cache(Base):
    """Generic key-value cache with TTL per source."""

    __tablename__ = "cache"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


# ─── Users ────────────────────────────────────────────────────────────
class User(Base):
    """Telegram users whitelisted to interact with the bot."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    preferences: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    watchlist_items: Mapped[list[WatchlistItem]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    price_alerts: Mapped[list[PriceAlert]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


# ─── Watchlist ────────────────────────────────────────────────────────
class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "ticker", name="uq_watchlist_user_ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="watchlist_items")


# ─── Price alerts ─────────────────────────────────────────────────────
class PriceAlert(Base):
    __tablename__ = "price_alerts"
    __table_args__ = (CheckConstraint("operator IN ('>', '<')", name="ck_price_alert_operator"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    operator: Mapped[str] = mapped_column(String(1), nullable=False)
    target_price: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    user: Mapped[User] = relationship(back_populates="price_alerts")


# ─── Sent alerts (dedup) ──────────────────────────────────────────────
class SentAlert(Base):
    """Deduplication index — prevents sending the same alert twice."""

    __tablename__ = "sent_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False, index=True
    )


# ─── AI calls log ─────────────────────────────────────────────────────
class AICall(Base):
    """One row per LLM invocation; fed to UsageTracker for quota checks."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False, index=True
    )
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ─── Source calls log ─────────────────────────────────────────────────
class SourceCall(Base):
    """One row per upstream data-source hit; used for metrics and debugging."""

    __tablename__ = "source_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ─── Trading signals (FASE 7) ─────────────────────────────────────────
class SignalRow(Base):
    """Persisted strategy thesis + lifecycle state.

    Mapped 1-to-1 with :class:`mib.trading.signals.Signal` plus the
    extra row-level fields (``id``, ``status``, ``status_updated_at``).
    The dataclass is the in-memory thesis; this is its DB row.

    Schema design notes:

    - Composite index on ``(strategy_id, generated_at)``. The hot
      backtest query in FASE 12 is "all signals from
      ``scanner.<preset>.<version>`` between dates X–Y", which would
      otherwise full-scan once we cross a few thousand rows.
    - ``indicators_json`` is :class:`sqlalchemy.JSON`, not Text. SQLite
      ≥ 3.38 exposes the ``->`` / ``->>`` operators, so backtest
      analysis can ``WHERE indicators_json->>'rsi_14' < 25`` without a
      Python parse step on every row.
    - Side and status sets are policed with check constraints so the
      DB rejects typos that the application layer might let slip.
    """

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_strategy_generated", "strategy_id", "generated_at"),
        CheckConstraint(
            "side IN ('long', 'short', 'flat')", name="ck_signals_side"
        ),
        CheckConstraint(
            "status IN ('pending', 'expired', 'consumed', 'cancelled')",
            name="ck_signals_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_low: Mapped[float] = mapped_column(Float, nullable=False)
    entry_high: Mapped[float] = mapped_column(Float, nullable=False)
    invalidation: Mapped[float] = mapped_column(Float, nullable=False)
    target_1: Mapped[float] = mapped_column(Float, nullable=False)
    target_2: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    indicators_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence_ai: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    status_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # FASE 8.1: TTL — signal becomes 'expired' once expires_at < now and
    # status is still 'pending'. Default = generated_at + 4 × timeframe_bars,
    # computed at insert time by the repository (ttl_bars override allowed).
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )


class SignalStatusEvent(Base):
    """Append-only event log for transitions of :class:`SignalRow.status`.

    Per ``ROADMAP.md`` Parte 0 append-only mandate: the mutable
    ``signals.status`` column is a denormalised cache of the latest
    event. Every transition writes a row here in the SAME transaction
    that updates the cache.

    The application-level helper :meth:`SignalRepository.transition`
    is the only allowed mutation path. Direct ``UPDATE`` on
    ``signals.status`` is forbidden by convention.
    """

    __tablename__ = "signal_status_events"
    __table_args__ = (
        Index("ix_signal_status_events_signal_id", "signal_id"),
        Index("ix_signal_status_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=False
    )
    # NULL on the "created" event (no prior state).
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    # One of: created | approved | cancelled | expired | consumed | reconciled
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # user:<telegram_id> | job:<job_name> | system
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


# ─── Trades (append-only, FASE 9.4) ──────────────────────────────────
class TradeRow(Base):
    """The position-level lifecycle: pending → open → closed | failed.

    One trade per signal (UNIQUE on signal_id). Joins to ``orders`` via
    the FK that FASE 9.4's migration adds to ``orders.trade_id``.

    ``realized_pnl_quote`` is populated on close (entry_price × size
    minus exit_price × size, with sign per side, minus fees).
    ``fees_paid_quote`` accumulates as fills come in.
    """

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("signal_id", name="uq_trades_signal_id"),
        Index("ix_trades_status_opened_at", "status", "opened_at"),
        Index(
            "ix_trades_closed_at",
            "closed_at",
            sqlite_where=Column("closed_at").is_not(None),  # type: ignore[arg-type]
        ),
        CheckConstraint("side IN ('long', 'short')", name="ck_trades_side"),
        CheckConstraint(
            "status IN ('pending', 'open', 'closed', 'failed')",
            name="ck_trades_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    size: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=8), nullable=False
    )
    entry_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=8), nullable=False
    )
    exit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=8), nullable=True
    )
    stop_loss_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=8), nullable=False
    )
    take_profit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=8), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    realized_pnl_quote: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=8), nullable=True
    )
    fees_paid_quote: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=8), nullable=False, default=Decimal(0)
    )
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class TradeStatusEvent(Base):
    """Append-only event log for :class:`TradeRow.status` transitions."""

    __tablename__ = "trade_status_events"
    __table_args__ = (
        Index("ix_trade_status_events_trade_id", "trade_id"),
        Index("ix_trade_status_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    # created | opened | closed | failed | reconciled
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


# ─── Orders (append-only, FASE 9.2) ──────────────────────────────────
class OrderRow(Base):
    """Persisted order placed (or attempted) on an exchange.

    The status column is the denormalised cache of the latest event in
    :class:`OrderStatusEvent`. Mutations go through
    :class:`mib.trading.order_repo.OrderRepository` which writes the
    audit row and updates the cache atomically.

    ``client_order_id`` is the idempotency key: the executor generates
    a deterministic id per signal+params combination so a retry hits
    the UNIQUE constraint and short-circuits to the existing row.
    ``trade_id`` is nullable until FASE 9.4 backpopulates it once the
    matching :class:`TradeRow` exists.
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
        Index("ix_orders_signal_id_created_at", "signal_id", "created_at"),
        Index(
            "ix_orders_exchange_order_id",
            "exchange_order_id",
            sqlite_where=Column("exchange_order_id").is_not(None),  # type: ignore[arg-type]
        ),
        CheckConstraint(
            "type IN ('limit', 'market', 'stop_market', 'stop_limit')",
            name="ck_orders_type",
        ),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_orders_side"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # FK to trades(id) is added by FASE 9.4's migration via
    # batch_alter_table once the trades table exists. Declared here
    # so future autogenerate runs don't try to drop the constraint.
    # Backpopulated by ``link_orders_to_trade`` when the matching
    # trade transitions pending → open.
    trade_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=True
    )
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=False
    )
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    exchange_id: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=8), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=8), nullable=False
    )
    reduce_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    raw_response_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OrderStatusEvent(Base):
    """Append-only event log for :class:`OrderRow.status` transitions."""

    __tablename__ = "order_status_events"
    __table_args__ = (
        Index("ix_order_status_events_order_id", "order_id"),
        Index("ix_order_status_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24), nullable=False)
    # created | submitted | partially_filled | filled | cancelled | rejected | failed | reconciled
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


# ─── Trading state (singleton, FASE 8.3) ──────────────────────────────
class TradingState(Base):
    """Single-row config table holding the runtime kill switch + DD tracker.

    The ``id = 1`` invariant is enforced via CHECK so any attempt to
    insert a second row is rejected at the DB level. All mutations
    flow through ``mib.trading.risk.state.TradingStateService.update``
    which records ``last_modified_by`` (the actor) for audit.
    """

    __tablename__ = "trading_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_trading_state_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    daily_dd_max_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.03)
    total_dd_max_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    killed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_modified_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    last_modified_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system"
    )


# ─── Risk decisions (append-only, FASE 8.3) ──────────────────────────
class RiskDecisionRow(Base):
    """Append-only log of every RiskManager evaluation.

    No row is ever UPDATEd or DELETEd in this table. Re-evaluating
    the same signal produces a new row with ``version = previous + 1``
    so the full decision history is recoverable. The composite
    UNIQUE constraint on ``(signal_id, version)`` makes concurrent
    appends with the same version impossible at the DB level —
    callers detect the conflict and retry with a fresh version.
    """

    __tablename__ = "risk_decisions"
    __table_args__ = (
        Index("ix_risk_decisions_signal_id", "signal_id"),
        UniqueConstraint(
            "signal_id", "version", name="uq_risk_decisions_signal_version"
        ),
        CheckConstraint("version >= 1", name="ck_risk_decisions_version_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gate_results_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    sized_amount_quote: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=8), nullable=True
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ─── Reconciliation (FASE 9.5) ────────────────────────────────────────
class PortfolioSnapshotRow(Base):
    """Persisted snapshot of :class:`PortfolioSnapshot` for diagnostics.

    Written by the reconciler each run so post-incident analysis can
    rebuild "what did the bot believe equity was at 14:32 UTC?". The
    reconciler also uses the previous snapshot to detect a balance
    drift > 1% relative to the exchange's reported equity.

    Append-only: never UPDATE, never DELETE. ``balances_json`` and
    ``positions_json`` carry the full structured payload for replay.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("ix_portfolio_snapshots_taken_at", "taken_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    equity_quote: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=8), nullable=False
    )
    balances_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    positions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )


class ReconcileRunRow(Base):
    """One reconciliation pass, summarised + raw discrepancy payload.

    The reconciler queries the exchange for open orders/positions,
    diffs against ``orders`` + ``trades`` rows, and writes a row here
    with three counters (orphan_exchange, orphan_db, balance_drift)
    plus a JSON list of every individual discrepancy. Operators can
    page through history via ``/reconcile`` or via direct DB.
    """

    __tablename__ = "reconcile_runs"
    __table_args__ = (
        Index("ix_reconcile_runs_started_at", "started_at"),
        CheckConstraint(
            "status IN ('ok', 'discrepancies', 'error')",
            name="ck_reconcile_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    orphan_exchange_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    orphan_db_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    balance_drift_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    discrepancies_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    portfolio_snapshot_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("portfolio_snapshots.id"), nullable=True
    )


# ─── Processed news (dedup) ───────────────────────────────────────────
class ProcessedNews(Base):
    __tablename__ = "processed_news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False, index=True
    )
