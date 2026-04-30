"""``GET /portfolio`` — return the current PortfolioSnapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from mib.api.dependencies import get_portfolio_state
from mib.models.portfolio import PortfolioSnapshot
from mib.trading.portfolio import PortfolioState

router = APIRouter(tags=["trading"])


@router.get("/portfolio", response_model=PortfolioSnapshot)
async def get_portfolio(
    state: PortfolioState = Depends(get_portfolio_state),
) -> PortfolioSnapshot:
    """Cached portfolio snapshot, auto-refreshed if older than TTL."""
    return await state.snapshot()
