"""Operations primitives (FASE 14).

Pre-flight checklists, /go_live 2FA flow, daily reports, /wind_down
graceful exit. None of these flip ``trading_enabled``: the activation
is **explicit operator decision** at /go_live + /go_live_confirm.
"""

from mib.operations.preflight import (
    CheckResult,
    CheckSeverity,
    PreflightReport,
    run_preflight,
)

__all__ = [
    "CheckResult",
    "CheckSeverity",
    "PreflightReport",
    "run_preflight",
]
