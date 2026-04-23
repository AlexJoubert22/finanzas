"""`GET /macro` — market-wide KPIs snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from mib.api.dependencies import get_macro_service
from mib.logger import logger
from mib.models.macro import MacroResponse
from mib.services.macro import MacroService

router = APIRouter(tags=["macro"])


@router.get("/macro", response_model=MacroResponse)
async def get_macro(service: MacroService = Depends(get_macro_service)) -> MacroResponse:
    """Return SPX, VIX, DXY, 10Y yield and BTC dominance."""
    try:
        return await service.snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET /macro failed: {}", exc)
        raise HTTPException(status_code=502, detail="No se pudo generar el macro snapshot.") from exc
