"""SQLAlchemy ORM models — spec §7.

All tables are defined here so they are visible to Alembic's
autogenerate machinery via `target_metadata = Base.metadata`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
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
