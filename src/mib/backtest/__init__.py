"""Backtester (FASE 12).

The cardinal rule of this package is **honest replay**: the same
:class:`mib.trading.strategy.StrategyEngine` evaluators that production
uses run unchanged inside the backtester. The only piece that swaps is
the executor — production calls the real :class:`OrderExecutor`, the
backtester calls a :class:`FillSimulator`. Anything that re-implements
strategy logic here would defeat the whole point of having a
backtester.

Sub-commits land progressively:

- 12.1 — engine skeleton + replay loop + FillSimulator Protocol
- 12.2 — realistic FillSimulator (slippage + partial fills + latency)
- 12.3 — metrics (profit factor, Sharpe, Sortino, expectancy, R-dist)
- 12.4 — equity curves with/without fees
- 12.5 — ``backtest_runs`` table (isolated from prod)
- 12.6 — /backtest endpoint + Telegram
- 12.7 — walk-forward optimisation + parameter stability scoring
- 12.8 — look-ahead bias test (BLOCKING in CI)
"""

from mib.backtest.engine import Backtester, BacktestFeed, BacktestReport
from mib.backtest.fill_simulator import (
    FillSimulationResult,
    FillSimulator,
    NoFillSimulator,
    SlippageConfig,
    SlippageFillSimulator,
)
from mib.backtest.types import BacktestBar, BacktestSettings, BacktestTrade

__all__ = [
    "Backtester",
    "BacktestBar",
    "BacktestFeed",
    "BacktestReport",
    "BacktestSettings",
    "BacktestTrade",
    "FillSimulationResult",
    "FillSimulator",
    "NoFillSimulator",
    "SlippageConfig",
    "SlippageFillSimulator",
]
