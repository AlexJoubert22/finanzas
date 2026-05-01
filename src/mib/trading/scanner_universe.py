"""Loader + validator for ``config/scanner_universe.yaml``.

Defines the active universe and per-preset cron cadences for the
PAPER-mode scanner. Loaded once at boot via
:meth:`ScannerUniverse.from_yaml`. A malformed file is fatal — the
operator sees the problem during startup instead of a silent
skipped-scan situation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from mib.services.scanner import PresetName

#: Presets we recognise for cron scheduling. Anything else in the YAML
#: under ``scanner_schedules`` is rejected at load time.
ALLOWED_PRESETS: Final[tuple[PresetName, ...]] = (
    "oversold",
    "breakout",
    "trending",
)


class ScannerUniverseConfigError(ValueError):
    """Raised when ``config/scanner_universe.yaml`` is missing, empty,
    or structurally malformed. Caught at startup so the operator knows
    before the scheduler starts firing dead jobs.
    """


@dataclass(frozen=True)
class PresetSchedule:
    """Cron expression + timeframe for one scanner preset."""

    cron: str
    timeframe: str


@dataclass(frozen=True)
class ScannerUniverse:
    """Universe + cron schedules for the PAPER-mode scanner."""

    paper_universe: tuple[str, ...]
    schedules: dict[PresetName, PresetSchedule]

    @classmethod
    def from_yaml(cls, path: Path) -> ScannerUniverse:
        if not path.exists():
            raise ScannerUniverseConfigError(
                f"scanner universe config not found: {path}"
            )
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict) or not raw:
            raise ScannerUniverseConfigError(
                f"scanner universe config must be a non-empty mapping: {path}"
            )

        universe = _parse_universe(raw.get("paper_mode_universe"))
        schedules = _parse_schedules(raw.get("scanner_schedules"))
        return cls(paper_universe=universe, schedules=schedules)


def _parse_universe(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ScannerUniverseConfigError(
            "'paper_mode_universe' must be a non-empty list of tickers"
        )
    if not all(isinstance(t, str) and t.strip() for t in value):
        raise ScannerUniverseConfigError(
            "'paper_mode_universe' tickers must be non-empty strings"
        )
    cleaned = tuple(t.strip() for t in value)
    if len(set(cleaned)) != len(cleaned):
        raise ScannerUniverseConfigError(
            "'paper_mode_universe' contains duplicate tickers"
        )
    return cleaned


def _parse_schedules(value: object) -> dict[PresetName, PresetSchedule]:
    if not isinstance(value, dict) or not value:
        raise ScannerUniverseConfigError(
            "'scanner_schedules' must be a non-empty mapping"
        )
    schedules: dict[PresetName, PresetSchedule] = {}
    for preset, spec in value.items():
        if preset not in ALLOWED_PRESETS:
            raise ScannerUniverseConfigError(
                f"unknown preset {preset!r}; allowed: {ALLOWED_PRESETS}"
            )
        if not isinstance(spec, dict):
            raise ScannerUniverseConfigError(
                f"preset {preset!r}: spec must be a mapping"
            )
        cron = spec.get("cron")
        timeframe = spec.get("timeframe")
        if not isinstance(cron, str) or not cron.strip():
            raise ScannerUniverseConfigError(
                f"preset {preset!r}: 'cron' must be a non-empty string"
            )
        if not isinstance(timeframe, str) or not timeframe.strip():
            raise ScannerUniverseConfigError(
                f"preset {preset!r}: 'timeframe' must be a non-empty string"
            )
        # Cheap sanity check: a 5-field cron expression.
        if len(cron.split()) != 5:
            raise ScannerUniverseConfigError(
                f"preset {preset!r}: 'cron' must have 5 fields (got {cron!r})"
            )
        schedules[preset] = PresetSchedule(
            cron=cron.strip(), timeframe=timeframe.strip()
        )
    return schedules


def default_universe_path() -> Path:
    """Standard location of the YAML, relative to repo root."""
    return Path(__file__).resolve().parents[3] / "config" / "scanner_universe.yaml"
