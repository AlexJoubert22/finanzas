"""Structured JSON logging via loguru.

Per spec §12: no `print()`; always `logger`. Logs go to stdout so
Docker can collect them via the json-file driver.
"""

from __future__ import annotations

import sys

from loguru import logger

from mib.config import get_settings


def configure_logging() -> None:
    """Install the JSON sink on stdout.

    Called exactly once from `main.py` before any other module emits logs.
    Removes the default loguru sink (which prints colored pretty-logs to
    stderr) so only our JSON sink remains.
    """
    settings = get_settings()
    logger.remove()

    # In development we keep the human-friendly colored format;
    # production always emits JSON (one object per line).
    if settings.app_env == "development":
        logger.add(
            sys.stdout,
            level=settings.log_level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
            ),
            backtrace=True,
            diagnose=True,
        )
        return

    logger.add(
        sys.stdout,
        level=settings.log_level,
        serialize=True,  # JSON output (one record per line).
        backtrace=True,
        diagnose=False,  # diagnose=True would leak variable values
    )


__all__ = ["configure_logging", "logger"]
