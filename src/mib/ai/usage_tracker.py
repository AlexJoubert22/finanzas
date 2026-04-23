"""Per-provider daily quota tracker backed by the ``ai_calls`` table.

Usage:
    tracker = UsageTracker()
    if await tracker.is_over_limit(ProviderId.GROQ, daily_limit=14000):
        skip_groq()

Today's window is UTC-aligned (midnight to midnight) for simplicity —
matches how providers publish their free-tier quotas (Groq, OpenRouter,
Gemini all measure in calendar UTC days).

Per spec §12, this module is subject to ``mypy --strict``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from mib.ai.models import ProviderId
from mib.db.models import AICall
from mib.db.session import async_session_factory


class UsageTracker:
    """Counts calls per provider over the current UTC day."""

    def __init__(self, *, skip_ratio: float = 0.90) -> None:
        """Create a tracker.

        Args:
            skip_ratio: At which quota fraction the router should start
                skipping the provider. Default 0.90 per spec §5.
        """
        self._skip_ratio = skip_ratio

    async def calls_today(self, provider: ProviderId) -> int:
        """Count successful + failed calls made today for ``provider``."""
        midnight_utc = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        async with async_session_factory() as session:
            stmt = (
                select(func.count(AICall.id))
                .where(AICall.provider == provider.value)
                .where(AICall.timestamp >= midnight_utc)
            )
            n = (await session.execute(stmt)).scalar_one()
            return int(n or 0)

    async def is_over_limit(self, provider: ProviderId, *, daily_limit: int) -> bool:
        """True if ``provider`` has used ≥ ``skip_ratio`` of its daily quota."""
        if daily_limit <= 0:
            return False
        used = await self.calls_today(provider)
        return used >= int(daily_limit * self._skip_ratio)

    async def usage_snapshot(self, limits: dict[ProviderId, int]) -> dict[str, float]:
        """Return ``{provider: fraction_used}`` ∈ [0, 1] for ``/health`` JSON."""
        out: dict[str, float] = {}
        for provider, limit in limits.items():
            if limit <= 0:
                continue
            used = await self.calls_today(provider)
            out[provider.value] = round(used / limit, 4)
        return out

    async def log_call(
        self,
        *,
        provider: ProviderId,
        task_type: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Append a row to ``ai_calls`` — used by AIRouter after each attempt."""
        async with async_session_factory() as session:
            session.add(
                AICall(
                    timestamp=datetime.now(UTC).replace(tzinfo=None),
                    task_type=task_type,
                    provider=provider.value,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    success=success,
                    error=error,
                )
            )
            await session.commit()

    async def prune_older_than(self, days: int = 30) -> int:
        """Retention: drop ``ai_calls`` rows older than ``days``. Returns count deleted."""
        from sqlalchemy import delete as sa_delete

        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        async with async_session_factory() as session:
            result = await session.execute(sa_delete(AICall).where(AICall.timestamp < cutoff))
            await session.commit()
            # DML results expose rowcount at runtime even if the typing
            # on the base Result[Any] class doesn't promise it.
            return int(getattr(result, "rowcount", 0) or 0)
