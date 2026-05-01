"""Tests for the scanner_universe.yaml loader and scheduled jobs."""

from __future__ import annotations

from pathlib import Path

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from mib.trading.jobs.scanner_jobs import register_scanner_jobs
from mib.trading.scanner_universe import (
    ScannerUniverse,
    ScannerUniverseConfigError,
    default_universe_path,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "scanner_universe.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ─── Loader ──────────────────────────────────────────────────────────


def test_real_yaml_parses(tmp_path: Path) -> None:
    """The repo-shipped YAML must parse without error."""
    universe = ScannerUniverse.from_yaml(default_universe_path())
    assert len(universe.paper_universe) == 10
    assert "BTC/USDT" in universe.paper_universe
    assert "POL/USDT" in universe.paper_universe
    assert set(universe.schedules.keys()) == {
        "oversold", "breakout", "trending"
    }
    assert universe.schedules["oversold"].cron == "30 * * * *"
    assert universe.schedules["breakout"].cron == "30 0,4,8,12,16,20 * * *"
    assert universe.schedules["trending"].cron == "30 0 * * *"
    assert universe.schedules["oversold"].timeframe == "1h"
    assert universe.schedules["breakout"].timeframe == "4h"
    assert universe.schedules["trending"].timeframe == "1d"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ScannerUniverseConfigError, match="not found"):
        ScannerUniverse.from_yaml(tmp_path / "nope.yaml")


def test_empty_file_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "")
    with pytest.raises(ScannerUniverseConfigError, match="non-empty mapping"):
        ScannerUniverse.from_yaml(p)


def test_missing_universe_key_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "scanner_schedules:\n  oversold:\n    cron: '* * * * *'\n"
        "    timeframe: '1h'\n",
    )
    with pytest.raises(ScannerUniverseConfigError, match="paper_mode_universe"):
        ScannerUniverse.from_yaml(p)


def test_duplicate_tickers_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "paper_mode_universe:\n  - BTC/USDT\n  - BTC/USDT\n"
        "scanner_schedules:\n  oversold:\n    cron: '* * * * *'\n"
        "    timeframe: '1h'\n",
    )
    with pytest.raises(ScannerUniverseConfigError, match="duplicate"):
        ScannerUniverse.from_yaml(p)


def test_unknown_preset_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "paper_mode_universe:\n  - BTC/USDT\n"
        "scanner_schedules:\n  bogus:\n    cron: '* * * * *'\n"
        "    timeframe: '1h'\n",
    )
    with pytest.raises(ScannerUniverseConfigError, match="unknown preset"):
        ScannerUniverse.from_yaml(p)


def test_bad_cron_field_count_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "paper_mode_universe:\n  - BTC/USDT\n"
        "scanner_schedules:\n  oversold:\n    cron: '0 6'\n"
        "    timeframe: '1h'\n",
    )
    with pytest.raises(ScannerUniverseConfigError, match="5 fields"):
        ScannerUniverse.from_yaml(p)


# ─── Scheduler registration ──────────────────────────────────────────


def test_register_scanner_jobs_uses_cron_triggers() -> None:
    """Each preset registers a job with a CronTrigger built from YAML."""
    sched = AsyncIOScheduler(timezone="UTC")
    universe = register_scanner_jobs(sched)
    assert len(universe.schedules) == 3

    job_ids = {j.id for j in sched.get_jobs()}
    assert {
        "scanner_oversold",
        "scanner_breakout",
        "scanner_trending",
    } <= job_ids
    for preset in ("oversold", "breakout", "trending"):
        job = sched.get_job(f"scanner_{preset}")
        assert job is not None
        assert isinstance(job.trigger, CronTrigger)


@pytest.mark.asyncio
async def test_runner_skips_when_bot_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner short-circuits when the bot isn't running."""
    sched = AsyncIOScheduler(timezone="UTC")
    register_scanner_jobs(sched)

    from mib.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "fake-token")
    monkeypatch.setattr(settings, "operator_telegram_id", 12345)
    from mib.telegram import bot as bot_mod
    monkeypatch.setattr(bot_mod, "_app", None)

    job = sched.get_job("scanner_oversold")
    assert job is not None
    # Run the bound coroutine directly — must complete with no exception
    # and no signal-pipeline side effects.
    await job.func()


@pytest.mark.asyncio
async def test_runner_skips_when_operator_id_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sched = AsyncIOScheduler(timezone="UTC")
    register_scanner_jobs(sched)
    from mib.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "fake-token")
    monkeypatch.setattr(settings, "operator_telegram_id", 0)

    job = sched.get_job("scanner_breakout")
    assert job is not None
    await job.func()
