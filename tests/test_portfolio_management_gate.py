from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.config_loader import load_config
from src.application.portfolio_management import (
    PORTFOLIO_MANAGEMENT_DISABLED,
    PORTFOLIO_MANAGEMENT_INCOMPATIBLE,
    normalize_portfolio_management_config,
    portfolio_management_failure_code,
    resolve_portfolio_management_client,
)
from src.infrastructure.portfolio_management_client import PortfolioManagementHTTPError


def test_old_runtime_alias_loads_as_canonical_boolean(tmp_path: Path) -> None:
    path = tmp_path / "config.us.json"
    path.write_text(
        json.dumps(
            {
                "trade_intake": {
                    "holdings_sync": {
                        "enabled": True,
                        "max_attempts": 99,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    logs = []

    loaded = load_config(
        base=tmp_path,
        config_path=path,
        is_scheduled=False,
        log=logs.append,
        validate_config_fn=lambda _cfg: None,
    )

    assert loaded["portfolio_management"] == {"enabled": True}
    assert "holdings_sync" not in loaded["trade_intake"]
    assert logs == [
        "[WARN] TRADE_INTAKE_HOLDINGS_SYNC_DEPRECATED: use "
        "portfolio_management.enabled; ignored keys: max_attempts"
    ]


def test_gate_defaults_disabled_and_never_opens_transport() -> None:
    calls = []
    assert (
        resolve_portfolio_management_client(
            {},
            urlopen_fn=lambda *_args, **_kwargs: calls.append(True),
        )
        == PORTFOLIO_MANAGEMENT_DISABLED
    )
    assert calls == []

    with pytest.raises(ValueError, match="must be a boolean"):
        resolve_portfolio_management_client(
            {"portfolio_management": {"enabled": "true"}},
            urlopen_fn=lambda *_args, **_kwargs: calls.append(True),
        )
    assert calls == []


@pytest.mark.parametrize(
    ("config", "error"),
    [
        (
            {
                "portfolio_management": {"enabled": True},
                "trade_intake": {"holdings_sync": {"enabled": True}},
            },
            "cannot both be set",
        ),
        (
            {"trade_intake": {"holdings_sync": {"mystery": 1}}},
            "unsupported keys",
        ),
        (
            {"portfolio_management": {"enabled": False, "url": "x"}},
            "unsupported keys",
        ),
    ],
)
def test_gate_rejects_conflicts_and_unknown_keys(config, error) -> None:
    with pytest.raises(ValueError, match=error):
        normalize_portfolio_management_config(config)


def test_validation_error_means_deployed_contract_is_incompatible() -> None:
    assert portfolio_management_failure_code(
        PortfolioManagementHTTPError("invalid request", status=422)
    ) == PORTFOLIO_MANAGEMENT_INCOMPATIBLE
