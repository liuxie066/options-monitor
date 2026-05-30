from __future__ import annotations


PURE_READ_TOOLS = frozenset(
    {
        "runtime_status",
        "healthcheck",
        "option_positions_read",
        "close_advice_read",
        "monthly_income_report",
        "runtime_runs",
        "runtime_logs",
        "config_validate",
    }
)


__all__ = ["PURE_READ_TOOLS"]
