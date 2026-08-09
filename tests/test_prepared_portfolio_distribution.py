from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _config(
    account: str,
    *,
    provider: str = "portfolio_management",
    enabled: bool = True,
    holdings_account: str | None = None,
) -> dict:
    config = {
        "accounts": [account],
        "portfolio": {"account": account},
        "account_settings": {account: {}},
        "ai_decision_advice": {
            "enabled": enabled,
            "portfolio_distribution": {"provider": provider},
        },
        "symbols": [],
    }
    if holdings_account is not None:
        config["account_settings"][account]["holdings_account"] = (
            holdings_account
        )
    return config


def _authority(tmp_path: Path, run_id: str, account: str, config: dict):
    from src.application.tick_run_workspace import publish_account_run_config

    return publish_account_run_config(
        base=tmp_path,
        run_id=run_id,
        account=account,
        config=config,
    )


def _receipt(account: str, *, assets: list[dict] | None = None) -> dict:
    return {
        "success": True,
        "accounts": [account],
        "freshness": {
            "status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": "2026-08-09T11:59:00Z",
            "dataset_ids": ["pm.prices", "pm.holdings"],
            "reason_codes": [],
        },
        "retrieved_at_utc": "2026-08-09T12:00:00Z",
        "by_asset": assets
        if assets is not None
        else [
            {
                "code": "NVDA",
                "name": "NVIDIA",
                "normalized_type": "stock",
                "currency": "USD",
                "quantity": 10,
                "value": 600.0,
                "ratio": 0.99,
                "accounts": {account: 10},
                "brokers": ["futu"],
                "breakdown": [
                    {
                        "account": account,
                        "broker": "futu",
                        "quantity": 10,
                        "value": 600.0,
                    }
                ],
            },
            {
                "code": "USD-MMF",
                "normalized_type": "cash",
                "currency": "USD",
                "quantity": 400,
                "value": 400.0,
                "accounts": {account: 400},
                "breakdown": [
                    {
                        "account": account,
                        "broker": "futu",
                        "quantity": 400,
                        "value": 400.0,
                    }
                ],
            },
        ],
        "total_value": 1.0,
        "errors": [],
    }


class _Client:
    def __init__(self, responses: dict[str, object], calls: list[tuple[str, float]]):
        self._responses = responses
        self._calls = calls

    def read_distribution(self, *, account: str, timeout: float):
        self._calls.append((account, timeout))
        value = self._responses[account]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def _prepare(
    tmp_path: Path,
    *,
    configs: dict[str, dict],
    responses: dict[str, object],
    calls: list[tuple[str, float]],
    run_id: str = "run-1",
):
    from src.application.prepared_portfolio_distribution import (
        prepare_portfolio_distributions,
    )

    authorities = {
        account: _authority(tmp_path, run_id, account, config)
        for account, config in configs.items()
    }
    batch = prepare_portfolio_distributions(
        base=tmp_path,
        run_id=run_id,
        account_configs=configs,
        account_config_authorities=authorities,
        timeout_sec=7,
        client_factory=lambda: _Client(responses, calls),
        now_fn=lambda: NOW,
    )
    return batch, authorities


@pytest.mark.parametrize(
    ("enabled", "provider", "reason"),
    [
        (False, "portfolio_management", "advice_disabled"),
        (True, "none", "provider_none"),
    ],
)
def test_disabled_or_none_publishes_unavailable_without_pm_call(
    tmp_path: Path,
    enabled: bool,
    provider: str,
    reason: str,
) -> None:
    calls: list[tuple[str, float]] = []
    config = _config("lx", enabled=enabled, provider=provider)

    batch, _authorities = _prepare(
        tmp_path,
        configs={"lx": config},
        responses={},
        calls=calls,
    )

    prepared = batch.by_account["lx"]
    assert calls == []
    assert batch.pm_read_count == 0
    assert prepared.status == "unavailable"
    assert prepared.reason == reason
    assert prepared.artifact_path is not None
    assert prepared.artifact_path.is_file()
    assert prepared.envelope["payload"]["assets"] == []


def test_fresh_trusted_distribution_is_account_bound_and_rederived(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, float]] = []
    config = _config("lx", holdings_account="Feishu EXT")

    batch, authorities = _prepare(
        tmp_path,
        configs={"lx": config},
        responses={"Feishu EXT": _receipt("Feishu EXT")},
        calls=calls,
    )

    prepared = batch.by_account["lx"]
    payload = prepared.envelope["payload"]
    authority = prepared.envelope["authority"]
    assert calls == [("Feishu EXT", 7.0)]
    assert batch.pm_read_count == 1
    assert prepared.status == "ready"
    assert authority["account"] == "lx"
    assert authority["mapped_pm_account"] == "Feishu EXT"
    assert authority["account_config_sha256"] == (
        authorities["lx"].account_config_sha256
    )
    assert payload["valuation_currency"] == "CNY"
    assert payload["derived"] == {
        "total_value": 1000.0,
        "asset_weights": {"NVDA": 0.6, "USD-MMF": 0.4},
        "currency_weights": {"USD": 1.0},
        "cash_and_mmf_weight": 0.4,
    }
    assert payload["assets"] == [
        {
            "code": "NVDA",
            "normalized_type": "stock",
            "currency": "USD",
            "quantity": 10.0,
            "value": 600.0,
        },
        {
            "code": "USD-MMF",
            "normalized_type": "cash",
            "currency": "USD",
            "quantity": 400.0,
            "value": 400.0,
        },
    ]
    assert "total_value" not in authority
    assert "brokers" not in payload["assets"][0]
    assert "accounts" not in payload["assets"][0]


def test_fresh_trusted_empty_distribution_is_ready_zero(tmp_path: Path) -> None:
    calls: list[tuple[str, float]] = []
    config = _config("lx")

    batch, _authorities = _prepare(
        tmp_path,
        configs={"lx": config},
        responses={"lx": _receipt("lx", assets=[])},
        calls=calls,
    )

    prepared = batch.by_account["lx"]
    assert prepared.status == "ready"
    assert prepared.envelope["payload"]["derived"] == {
        "total_value": 0.0,
        "asset_weights": {},
        "currency_weights": {},
        "cash_and_mmf_weight": 0.0,
    }


@pytest.mark.parametrize(
    ("freshness", "trust", "expected_status", "expected_reason", "keeps_assets"),
    [
        ("stale", "trusted", "degraded", "portfolio_stale", True),
        ("fresh", "partial", "degraded", "portfolio_partial", True),
        ("stale", "partial", "degraded", "portfolio_partial", True),
        ("unknown", "trusted", "unavailable", "portfolio_freshness_unknown", False),
        ("fresh", "untrusted", "unavailable", "portfolio_quality_untrusted", False),
        ("unavailable", "unavailable", "unavailable", "portfolio_quality_unavailable", False),
    ],
)
def test_quality_mapping_is_deterministic(
    tmp_path: Path,
    freshness: str,
    trust: str,
    expected_status: str,
    expected_reason: str,
    keeps_assets: bool,
) -> None:
    calls: list[tuple[str, float]] = []
    response = _receipt("lx")
    response["freshness"]["status"] = freshness
    response["freshness"]["trust_status"] = trust

    batch, _authorities = _prepare(
        tmp_path,
        configs={"lx": _config("lx")},
        responses={"lx": response},
        calls=calls,
        run_id=f"run-{freshness}-{trust}",
    )

    prepared = batch.by_account["lx"]
    assert prepared.status == expected_status
    assert prepared.reason == expected_reason
    assert bool(prepared.envelope["payload"]["assets"]) is keeps_assets


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda item: item.update(accounts=["sy"]), "pm_protocol_error"),
        (
            lambda item: item["by_asset"][0].update(
                breakdown=[{"account": "sy"}]
            ),
            "pm_protocol_error",
        ),
        (
            lambda item: item["by_asset"][0].update(value=float("nan")),
            "pm_protocol_error",
        ),
        (lambda item: item.update(errors=[{"error": "partial"}]), "pm_response_errors"),
    ],
)
def test_invalid_or_mixed_account_response_fails_closed(
    tmp_path: Path,
    mutator,
    reason: str,
) -> None:
    calls: list[tuple[str, float]] = []
    response = _receipt("lx")
    mutator(response)

    batch, _authorities = _prepare(
        tmp_path,
        configs={"lx": _config("lx")},
        responses={"lx": response},
        calls=calls,
    )

    prepared = batch.by_account["lx"]
    assert prepared.status == "unavailable"
    assert prepared.reason == reason
    assert prepared.envelope["payload"]["assets"] == []


def test_multi_account_reads_once_and_never_crosses_rows(tmp_path: Path) -> None:
    calls: list[tuple[str, float]] = []
    configs = {
        "lx": _config("lx", holdings_account="PM LX"),
        "sy": _config("sy", holdings_account="PM SY"),
    }

    batch, _authorities = _prepare(
        tmp_path,
        configs=configs,
        responses={
            "PM LX": _receipt("PM LX"),
            "PM SY": _receipt("PM SY", assets=[]),
        },
        calls=calls,
    )

    assert calls == [("PM LX", 7.0), ("PM SY", 7.0)]
    assert batch.pm_read_count == 2
    assert batch.by_account["lx"].envelope["authority"]["mapped_pm_account"] == "PM LX"
    assert batch.by_account["sy"].envelope["authority"]["mapped_pm_account"] == "PM SY"
    assert batch.by_account["lx"].envelope["payload"]["assets"]
    assert batch.by_account["sy"].envelope["payload"]["assets"] == []


def test_transport_failure_is_persisted_unavailable_and_counts_attempt(
    tmp_path: Path,
) -> None:
    from src.infrastructure.portfolio_management_client import (
        PortfolioManagementTransportError,
    )

    calls: list[tuple[str, float]] = []
    batch, _authorities = _prepare(
        tmp_path,
        configs={"lx": _config("lx")},
        responses={
            "lx": PortfolioManagementTransportError("connection refused")
        },
        calls=calls,
    )

    assert calls == [("lx", 7.0)]
    assert batch.pm_read_count == 1
    assert batch.by_account["lx"].status == "unavailable"
    assert batch.by_account["lx"].reason == "pm_transport_error"
    assert batch.by_account["lx"].artifact_path is not None


def test_same_run_adopts_write_once_artifact_without_second_pm_read(
    tmp_path: Path,
) -> None:
    from src.application.prepared_portfolio_distribution import (
        prepare_portfolio_distributions,
    )

    calls: list[tuple[str, float]] = []
    config = _config("lx")
    first, authorities = _prepare(
        tmp_path,
        configs={"lx": config},
        responses={"lx": _receipt("lx")},
        calls=calls,
    )
    second = prepare_portfolio_distributions(
        base=tmp_path,
        run_id="run-1",
        account_configs={"lx": config},
        account_config_authorities=authorities,
        timeout_sec=7,
        client_factory=lambda: _Client({"lx": _receipt("lx")}, calls),
        now_fn=lambda: datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc),
    )

    assert calls == [("lx", 7.0)]
    assert second.pm_read_count == 0
    assert first.by_account["lx"].artifact_sha256 == second.by_account["lx"].artifact_sha256
    assert first.by_account["lx"].envelope == second.by_account["lx"].envelope


def test_existing_unsafe_artifact_fails_soft_without_pm_read(
    tmp_path: Path,
) -> None:
    from src.application.prepared_portfolio_distribution import (
        PREPARED_PORTFOLIO_DISTRIBUTION_NAME,
        prepare_portfolio_distributions,
    )

    config = _config("lx")
    authority = _authority(tmp_path, "run-1", "lx", config)
    target = tmp_path / "untrusted.json"
    target.write_text("{}\n", encoding="utf-8")
    artifact = authority.state_path.parent / PREPARED_PORTFOLIO_DISTRIBUTION_NAME
    artifact.symlink_to(target)
    calls: list[tuple[str, float]] = []

    batch = prepare_portfolio_distributions(
        base=tmp_path,
        run_id="run-1",
        account_configs={"lx": config},
        account_config_authorities={"lx": authority},
        timeout_sec=7,
        client_factory=lambda: _Client({"lx": _receipt("lx")}, calls),
        now_fn=lambda: NOW,
    )

    assert calls == []
    assert batch.pm_read_count == 0
    assert batch.by_account["lx"].status == "unavailable"
    assert batch.by_account["lx"].reason == "artifact_invalid"
    assert batch.by_account["lx"].artifact_path is None


def test_loader_wraps_rehashed_invalid_row_as_typed_artifact_error(
    tmp_path: Path,
) -> None:
    from src.application.prepared_portfolio_distribution import (
        PreparedPortfolioDistributionError,
        load_prepared_portfolio_distribution,
    )

    calls: list[tuple[str, float]] = []
    config = _config("lx")
    batch, authorities = _prepare(
        tmp_path,
        configs={"lx": config},
        responses={"lx": _receipt("lx")},
        calls=calls,
    )
    artifact = batch.by_account["lx"].artifact_path
    assert artifact is not None
    envelope = json.loads(artifact.read_text(encoding="utf-8"))
    envelope["payload"]["assets"][0]["quantity"] = "invalid"
    canonical_payload = json.dumps(
        envelope["payload"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    envelope["integrity"]["payload_sha256"] = hashlib.sha256(
        canonical_payload
    ).hexdigest()
    artifact.write_text(
        json.dumps(envelope, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(
        PreparedPortfolioDistributionError,
        match="semantics are invalid",
    ):
        load_prepared_portfolio_distribution(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            expected_account_config_sha256=(
                authorities["lx"].account_config_sha256
            ),
            expected_mapped_pm_account="lx",
            expected_provider="portfolio_management",
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda envelope: envelope["authority"].update(extra="unexpected"),
        lambda envelope: envelope["authority"].update(
            reason="ready",
            validation={"status": "passed"},
        ),
    ],
)
def test_loader_rejects_open_or_impossible_unavailable_authority(
    tmp_path: Path,
    mutator,
) -> None:
    from src.application.prepared_portfolio_distribution import (
        PreparedPortfolioDistributionError,
        load_prepared_portfolio_distribution,
    )

    config = _config("lx", provider="none")
    batch, authorities = _prepare(
        tmp_path,
        configs={"lx": config},
        responses={},
        calls=[],
    )
    artifact = batch.by_account["lx"].artifact_path
    assert artifact is not None
    envelope = json.loads(artifact.read_text(encoding="utf-8"))
    mutator(envelope)
    artifact.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(PreparedPortfolioDistributionError):
        load_prepared_portfolio_distribution(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            expected_account_config_sha256=(
                authorities["lx"].account_config_sha256
            ),
            expected_mapped_pm_account="lx",
            expected_provider="none",
        )


def test_loader_rejects_payload_and_external_artifact_hash_tamper(
    tmp_path: Path,
) -> None:
    from src.application.prepared_portfolio_distribution import (
        PreparedPortfolioDistributionError,
        load_prepared_portfolio_distribution,
    )

    calls: list[tuple[str, float]] = []
    config = _config("lx")
    batch, authorities = _prepare(
        tmp_path,
        configs={"lx": config},
        responses={"lx": _receipt("lx")},
        calls=calls,
    )
    prepared = batch.by_account["lx"]
    assert prepared.artifact_path is not None
    original_hash = prepared.artifact_sha256

    envelope = json.loads(prepared.artifact_path.read_text(encoding="utf-8"))
    envelope["payload"]["assets"][0]["value"] = 999.0
    prepared.artifact_path.write_text(
        json.dumps(envelope, sort_keys=True),
        encoding="utf-8",
    )

    common = {
        "base": tmp_path,
        "run_id": "run-1",
        "account": "lx",
        "expected_account_config_sha256": authorities["lx"].account_config_sha256,
        "expected_mapped_pm_account": "lx",
        "expected_provider": "portfolio_management",
    }
    with pytest.raises(PreparedPortfolioDistributionError, match="artifact hash mismatch"):
        load_prepared_portfolio_distribution(
            **common,
            expected_artifact_sha256=original_hash,
        )
    with pytest.raises(PreparedPortfolioDistributionError, match="payload hash mismatch"):
        load_prepared_portfolio_distribution(**common)
