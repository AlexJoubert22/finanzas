"""Backtester engine — honest replay of StrategyEngine evaluators.

The replay loop walks each ticker's bars in chronological order and,
at every bar t, builds a synthetic :class:`SymbolResponse` whose
``candles`` array is the WINDOW of bars ``[0, t]`` and whose
``indicators`` is the snapshot valid AT bar t. This is exactly what
the production loop sees at runtime — there is **no peek at bar t+1
data inside the evaluator** (that's the look-ahead bias 12.8 will
prove with a CI test).

The same ``evaluate_oversold`` / ``evaluate_breakout`` /
``evaluate_trending`` functions production uses are imported from
:mod:`mib.trading.strategy`. Anything that re-implements strategy
logic in this file would defeat the whole point.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from mib.backtest.fill_simulator import (
    FillSimulationResult,
    FillSimulator,
    NoFillSimulator,
)
from mib.backtest.types import (
    BacktestBar,
    BacktestSettings,
    BacktestTrade,
    PresetName,
)
from mib.models.market import Quote, SymbolResponse
from mib.trading.signals import Signal
from mib.trading.strategy import (
    evaluate_breakout,
    evaluate_oversold,
    evaluate_trending,
)

logger = logging.getLogger(__name__)

#: Per-ticker time series the backtester replays. The bars must be
#: sorted ascending by timestamp; the engine does not re-sort.
BacktestFeed = Mapping[str, list[BacktestBar]]

_EVALUATORS = {
    "oversold": evaluate_oversold,
    "breakout": evaluate_breakout,
    "trending": evaluate_trending,
}


@dataclass(frozen=True)
class BacktestReport:
    """Outcome of one ``Backtester.run`` call.

    12.3 / 12.4 enrich this with metrics + equity curve. For 12.1 the
    report is the raw ledger; the metrics layer wraps it.
    """

    settings: BacktestSettings
    preset: PresetName
    universe: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    bars_processed: int
    trades: list[BacktestTrade] = field(default_factory=list)
    skipped_signals: int = 0
    """Signals the engine fired but couldn't enter (limit unfilled,
    out-of-data tail, etc.). Counted so 12.3 metrics can report a
    'execution rate' to the operator."""

    @property
    def total_realized_pnl_quote(self) -> Decimal:
        return sum(
            (t.realized_pnl_quote for t in self.trades), Decimal(0)
        )

    @property
    def total_fees_paid_quote(self) -> Decimal:
        return sum(
            (t.fees_paid_quote for t in self.trades), Decimal(0)
        )


# ─── Engine ─────────────────────────────────────────────────────────


class Backtester:
    """Replay a feed through the production strategy evaluators.

    Construct once per run; the simulator's RNG is reseeded inside
    :meth:`run` so two consecutive runs with the same settings produce
    identical reports.
    """

    def __init__(
        self,
        *,
        fill_simulator: FillSimulator | None = None,
    ) -> None:
        self._sim: FillSimulator = fill_simulator or NoFillSimulator()

    def run(
        self,
        *,
        preset: PresetName,
        feed: BacktestFeed,
        settings: BacktestSettings | None = None,
        k_invalidation: float = 1.5,
        r_multiples: tuple[float, float] = (1.0, 3.0),
    ) -> BacktestReport:
        """One full replay. Pure function over (feed, settings).

        Returns the trades simulated + the count of signals that fired
        but couldn't enter (e.g. limit didn't cross). Errors don't
        raise — a broken bar logs at WARN and is skipped so a single
        bad row doesn't poison a 6-month run.
        """
        evaluator = _EVALUATORS.get(preset)
        if evaluator is None:
            raise ValueError(f"unknown preset: {preset!r}")
        cfg = settings or BacktestSettings()
        self._sim.reseed(cfg.random_seed)
        started = datetime.now()

        trades: list[BacktestTrade] = []
        skipped = 0
        bars_processed = 0

        for ticker, bars in feed.items():
            if not bars:
                continue
            ticker_trades, ticker_skipped, ticker_bars = self._replay_ticker(
                ticker=ticker,
                bars=bars,
                preset=preset,
                cfg=cfg,
                k_invalidation=k_invalidation,
                r_multiples=r_multiples,
            )
            trades.extend(ticker_trades)
            skipped += ticker_skipped
            bars_processed += ticker_bars

        finished = datetime.now()
        return BacktestReport(
            settings=cfg,
            preset=preset,
            universe=tuple(feed.keys()),
            started_at=started,
            finished_at=finished,
            bars_processed=bars_processed,
            trades=trades,
            skipped_signals=skipped,
        )

    # ─── Per-ticker replay ─────────────────────────────────────────

    def _replay_ticker(
        self,
        *,
        ticker: str,
        bars: list[BacktestBar],
        preset: PresetName,
        cfg: BacktestSettings,
        k_invalidation: float,
        r_multiples: tuple[float, float],
    ) -> tuple[list[BacktestTrade], int, int]:
        """Walk one ticker's bars. At most one open position at a time
        per ticker — entries fired while a position is open are
        skipped (the production sizer would also reject them via
        max-concurrent gates).
        """
        evaluator = _EVALUATORS[preset]
        trades: list[BacktestTrade] = []
        open_position: _OpenPosition | None = None
        skipped_signals = 0
        bars_seen = 0

        for idx, bar in enumerate(bars):
            bars_seen += 1
            next_bar = bars[idx + 1] if idx + 1 < len(bars) else None

            # 1) If we have an open position, score exit first.
            if open_position is not None:
                exit_outcome = _score_exit(open_position, bar, next_bar)
                if exit_outcome is not None:
                    trade = _close_position(
                        open_position,
                        exit_outcome,
                        cfg=cfg,
                        ticker=ticker,
                    )
                    trades.append(trade)
                    open_position = None
                    # Fall through: a fresh signal can fire on the same
                    # bar that closed our previous trade.

            # 2) Build SymbolResponse, ask the evaluator if a signal fires.
            if open_position is not None:
                # Already in a position — production gates would reject
                # a new entry; mirror that behaviour to keep "honest".
                continue

            symbol_response = _build_symbol_response(
                ticker=ticker, bars_window=bars[: idx + 1]
            )
            try:
                signal = evaluator(
                    symbol_response,
                    k_invalidation=k_invalidation,
                    r_multiples=r_multiples,
                )
            except (ValueError, AttributeError, IndexError) as exc:
                logger.warning(
                    "backtest: evaluator crashed on %s bar %d: %s",
                    ticker,
                    idx,
                    exc,
                )
                continue

            if signal is None:
                continue

            # 3) Try to fill the entry order.
            entry_outcome = self._try_enter(
                signal=signal,
                current_bar=bar,
                next_bar=next_bar,
                cfg=cfg,
            )
            if entry_outcome is None:
                skipped_signals += 1
                continue

            open_position = _OpenPosition(
                ticker=ticker,
                signal=signal,
                entry_price=entry_outcome.fill_price,
                entry_at=entry_outcome.fill_at,
                size_base=entry_outcome.filled_amount,
                fees_so_far=entry_outcome.fees_paid_quote,
                opened_at_idx=idx,
            )

        # End of feed: close any still-open position at the last bar's close.
        if open_position is not None:
            last = bars[-1]
            close_price = Decimal(str(last.candle.close))
            fees_close = (
                close_price * open_position.size_base * cfg.fee_pct
            ).quantize(Decimal("0.00000001"))
            exit_outcome = _ExitOutcome(
                price=close_price,
                at=last.candle.timestamp,
                reason="end_of_data",
                fees=fees_close,
            )
            trades.append(
                _close_position(
                    open_position,
                    exit_outcome,
                    cfg=cfg,
                    ticker=ticker,
                )
            )

        return trades, skipped_signals, bars_seen

    def _try_enter(
        self,
        *,
        signal: Signal,
        current_bar: BacktestBar,
        next_bar: BacktestBar | None,
        cfg: BacktestSettings,
    ) -> FillSimulationResult | None:
        """Place a market entry and ask the simulator to fill it."""
        size_base = _size_for_signal(signal=signal, cfg=cfg)
        if size_base <= 0:
            return None
        side = "buy" if signal.side == "long" else "sell"
        result = self._sim.simulate_fill(
            side=side,  # type: ignore[arg-type]
            order_type="market",
            amount_base=size_base,
            limit_price=None,
            current_bar=current_bar,
            next_bar=next_bar,
            fee_pct=cfg.fee_pct,
        )
        return result if result.filled else None


# ─── Internal value types ───────────────────────────────────────────


@dataclass
class _OpenPosition:
    ticker: str
    signal: Signal
    entry_price: Decimal
    entry_at: datetime
    size_base: Decimal
    fees_so_far: Decimal
    opened_at_idx: int


@dataclass(frozen=True)
class _ExitOutcome:
    price: Decimal
    at: datetime
    reason: Literal["stop", "target", "timeout", "end_of_data"]
    fees: Decimal


def _score_exit(
    pos: _OpenPosition,
    current_bar: BacktestBar,
    next_bar: BacktestBar | None,
) -> _ExitOutcome | None:
    """Did the bar's range trip our stop or target?

    Conservative semantics for ambiguous bars (both stop and target
    inside the candle): assume the worse one fires first. This is the
    standard backtesting assumption — without intra-bar tick data we
    can't tell which fired first, and over-optimistic resolution
    leads to inflated metrics.
    """
    candle = current_bar.candle
    side = pos.signal.side
    stop = Decimal(str(pos.signal.invalidation))
    target = Decimal(str(pos.signal.target_1))
    high = Decimal(str(candle.high))
    low = Decimal(str(candle.low))
    fee_pct = Decimal("0.001")  # FillSimulator's fee_pct passthrough; resolved in close
    if side == "long":
        stop_hit = low <= stop
        target_hit = high >= target
    else:
        stop_hit = high >= stop
        target_hit = low <= target
    if stop_hit and target_hit:
        # Pessimistic: stop fires first.
        return _ExitOutcome(
            price=stop,
            at=candle.timestamp,
            reason="stop",
            fees=(stop * pos.size_base * fee_pct).quantize(Decimal("0.00000001")),
        )
    if stop_hit:
        return _ExitOutcome(
            price=stop,
            at=candle.timestamp,
            reason="stop",
            fees=(stop * pos.size_base * fee_pct).quantize(Decimal("0.00000001")),
        )
    if target_hit:
        return _ExitOutcome(
            price=target,
            at=candle.timestamp,
            reason="target",
            fees=(target * pos.size_base * fee_pct).quantize(Decimal("0.00000001")),
        )
    # No exit this bar; next_bar is unused at 12.1 (12.2 may use it
    # for fill-after-trigger semantics on stops).
    _ = next_bar
    return None


def _close_position(
    pos: _OpenPosition,
    outcome: _ExitOutcome,
    *,
    cfg: BacktestSettings,
    ticker: str,
) -> BacktestTrade:
    side = pos.signal.side
    if side == "long":
        gross = (outcome.price - pos.entry_price) * pos.size_base
    else:
        gross = (pos.entry_price - outcome.price) * pos.size_base
    total_fees = (pos.fees_so_far + outcome.fees).quantize(
        Decimal("0.00000001")
    )
    net = (gross - total_fees).quantize(Decimal("0.00000001"))
    return BacktestTrade(
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        strategy_id=pos.signal.strategy_id,
        size_base=pos.size_base,
        entry_price=pos.entry_price,
        entry_at=pos.entry_at,
        exit_price=outcome.price,
        exit_at=outcome.at,
        exit_reason=outcome.reason,
        realized_pnl_quote=net,
        fees_paid_quote=total_fees,
        invalidation_price=Decimal(str(pos.signal.invalidation)),
        target_1_price=Decimal(str(pos.signal.target_1)),
        target_2_price=(
            Decimal(str(pos.signal.target_2))
            if pos.signal.target_2 is not None
            else None
        ),
        bars_held=0,  # filled by 12.3 metrics if needed
        metadata={
            "preset_quote_currency": cfg.quote_currency,
            "k_invalidation_used": str(pos.signal.indicators.get("atr_14", "")),
        },
    )


# ─── Pure helpers ────────────────────────────────────────────────────


def _size_for_signal(*, signal: Signal, cfg: BacktestSettings) -> Decimal:
    """Position sizing: equity * risk_per_trade_pct / risk_per_unit.

    Mirrors :class:`mib.trading.sizing.PositionSizer` — same risk-
    based formula so the backtester sizes positions the way production
    will. In the FASE 12 phase we keep it simple: the equity is the
    initial capital (the engine doesn't compound across trades yet;
    the metrics layer 12.3 handles per-trade R-multiples).
    """
    entry = (
        Decimal(str(signal.entry_zone[0]))
        + Decimal(str(signal.entry_zone[1]))
    ) / Decimal(2)
    invalidation = Decimal(str(signal.invalidation))
    risk_per_unit = abs(entry - invalidation)
    if risk_per_unit <= 0:
        return Decimal(0)
    risk_capital = cfg.initial_capital_quote * cfg.risk_per_trade_pct
    size = (risk_capital / risk_per_unit).quantize(Decimal("0.00000001"))
    return max(size, Decimal(0))


def _build_symbol_response(
    *, ticker: str, bars_window: list[BacktestBar]
) -> SymbolResponse:
    """Build a :class:`SymbolResponse` from the windowed feed.

    The window's last bar's indicators are used as the snapshot — by
    construction the indicators were computed on candles up to and
    including bar_t, never beyond. The strategy evaluators consume
    this exactly the same way they do at runtime.
    """
    last = bars_window[-1]
    quote = Quote(
        ticker=ticker,
        kind="crypto",
        source="backtest",
        price=last.candle.close,
        change_24h_pct=None,
        currency=None,
        venue="backtest",
        timestamp=last.candle.timestamp,
    )
    return SymbolResponse(
        quote=quote,
        candles=[b.candle for b in bars_window],
        indicators=last.indicators,
        technical_rating=None,
        ai_analysis=None,
    )
