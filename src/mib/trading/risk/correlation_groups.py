"""Loader + lookups for correlation group definitions.

Used by :class:`mib.trading.risk.gates.correlation_group.CorrelationGroupGate`.
The YAML file ``config/correlation_groups.yaml`` is loaded once at
service construction; bot restart applies edits. We deliberately do
not hot-reload because correlation taxonomy is a strategy-level
decision that warrants a redeploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from mib.logger import logger


class CorrelationGroupsConfigError(ValueError):
    """Raised when the YAML file is malformed (missing keys, bad types,
    out-of-range caps). Caught at boot so the operator sees the
    problem before the scheduler kicks off.
    """


@dataclass(frozen=True)
class CorrelationGroup:
    """One named correlation cluster with its combined-exposure cap."""

    name: str
    members: frozenset[str]
    max_pct: float


class CorrelationGroups:
    """Loaded + validated set of correlation groups, with reverse index."""

    def __init__(self, groups: list[CorrelationGroup]) -> None:
        self._groups = list(groups)
        self._by_member: dict[str, list[CorrelationGroup]] = {}
        for g in self._groups:
            for m in g.members:
                self._by_member.setdefault(m, []).append(g)

    @classmethod
    def from_yaml(cls, path: Path) -> CorrelationGroups:
        if not path.exists():
            raise CorrelationGroupsConfigError(
                f"correlation groups config not found: {path}"
            )
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if not isinstance(raw, dict) or not raw:
            raise CorrelationGroupsConfigError(
                f"correlation groups config must be a non-empty mapping: {path}"
            )

        groups: list[CorrelationGroup] = []
        for name, spec in raw.items():
            if not isinstance(spec, dict):
                raise CorrelationGroupsConfigError(
                    f"group {name!r}: spec must be a mapping, got {type(spec).__name__}"
                )
            members_raw = spec.get("members")
            cap_raw = spec.get("group_max_pct")
            if not isinstance(members_raw, list) or not members_raw:
                raise CorrelationGroupsConfigError(
                    f"group {name!r}: 'members' must be a non-empty list"
                )
            if not all(isinstance(m, str) and m.strip() for m in members_raw):
                raise CorrelationGroupsConfigError(
                    f"group {name!r}: every member must be a non-empty string"
                )
            if not isinstance(cap_raw, (int, float)):
                raise CorrelationGroupsConfigError(
                    f"group {name!r}: 'group_max_pct' must be a number"
                )
            cap = float(cap_raw)
            if not (0.0 < cap <= 1.0):
                raise CorrelationGroupsConfigError(
                    f"group {name!r}: 'group_max_pct' must be in (0, 1] (got {cap})"
                )
            groups.append(
                CorrelationGroup(
                    name=str(name),
                    members=frozenset(members_raw),
                    max_pct=cap,
                )
            )

        # Warn on duplicate membership across groups (allowed but worth flagging).
        seen: dict[str, str] = {}
        for g in groups:
            for m in g.members:
                if m in seen:
                    logger.warning(
                        "correlation_groups: ticker {} appears in both '{}' and '{}'; "
                        "strictest cap will apply",
                        m,
                        seen[m],
                        g.name,
                    )
                else:
                    seen[m] = g.name

        return cls(groups)

    def groups_for_ticker(self, ticker: str) -> list[CorrelationGroup]:
        return list(self._by_member.get(ticker, []))

    @property
    def all_groups(self) -> tuple[CorrelationGroup, ...]:
        return tuple(self._groups)
