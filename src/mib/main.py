"""Entrypoint — orchestrates FastAPI, Telegram polling, and scheduled jobs.

Phase 1 only wires FastAPI; the Telegram bot and the APScheduler jobs
land in phase 5.
"""

from __future__ import annotations

import uvicorn

from mib.config import get_settings
from mib.logger import configure_logging, logger


def main() -> None:
    """Run the HTTP API under uvicorn.

    Single worker by design (spec §11bis) — the app is async and the
    RAM budget is tight. For local dev you can `uv run python -m mib.main`.
    """
    configure_logging()
    settings = get_settings()
    logger.info(
        "mib booting · env={} · host={}:{}",
        settings.app_env,
        settings.api_host,
        settings.api_port,
    )

    uvicorn.run(
        "mib.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        workers=1,
        reload=False,
        log_level=settings.log_level.lower(),
        # uvicorn's own access log is noisy and duplicates our structured
        # log; disable it and rely on FastAPI route-level logging added
        # in phase 2.
        access_log=False,
    )


if __name__ == "__main__":
    main()
