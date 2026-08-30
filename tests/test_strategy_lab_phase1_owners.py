from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.performance.account_fee_plan import (
    ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
    AccountFeePlanReceiptError,
    load_account_fee_plan_receipt,
)
from src.application.strategy_lab.service import (
    StrategyLabContextError,
    resolve_strategy_lab_context,
    resolve_strategy_lab_runtime_context,
)
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.research import handle_research_command


def _profile(tmp_path: Path) -> dict[str, object]:
    runtime = tmp_path / "runtime"
    return {
        "service_provider": "systemd",
        "repo_root": str(tmp_path / "repo"),
        "runtime_root": str(runtime),
        "accounts": ["lx"],
        "markets": ["hk"],
        "config_paths": {"hk": str(runtime / "config.hk.json")},
        "env_file": str(runtime / "options-monitor.env"),
        "strategy_lab_top1": {
            "enabled": True,
            "market": "hk",
            "account": "lx",
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
            "advance_interval": 300,
            "timeout_start_sec": 120,
        },
    }


def test_account_fee_plan_owner_keeps_strict_auditable_facts(tmp_path: Path) -> None:
    path = tmp_path / "fee-plan.json"
    payload = {
        "schema_version": ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
        "market": "HK",
        "account": "lx",
        "commission_free": True,
        "platform_fee": 15.0,
        "fee_plan_ref": "futu-hk-plan.v1",
        "observed_at_utc": "2026-08-30T01:00:00Z",
        "evidence_ref": "operator://futu/lx/fee-plan/2026-08-30",
        "evidence_sha256": "a" * 64,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    receipt = load_account_fee_plan_receipt(path)
    assert receipt["account"] == "lx"
    assert len(str(receipt["source_receipt_sha256"])) == 64

    path.write_text(json.dumps({**payload, "platform_fee": -1}), encoding="utf-8")
    with pytest.raises(AccountFeePlanReceiptError) as raised:
        load_account_fee_plan_receipt(path)
    assert raised.value.reason_code == "account_fee_plan_receipt_invalid"


def test_strategy_lab_context_owner_resolves_only_controlled_profile_paths(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)
    context = resolve_strategy_lab_context(profile)

    assert context["artifact_root"] == (tmp_path / "runtime/output_shared/research/strategy_lab")
    assert context["store_path"] == context["artifact_root"] / "experiments.sqlite3"
    assert (context["market"], context["account"]) == ("hk", "lx")
    assert context["opend_binding"] == {"host": "127.0.0.1", "port": 11111}
    assert context["opend_limiter_root"] == tmp_path / "runtime"
    assert context["tick_lock_path"] == tmp_path / "runtime/locks/tick-hk.lock"

    with pytest.raises(StrategyLabContextError, match="absolute path"):
        resolve_strategy_lab_context({**profile, "runtime_root": "runtime"})


@pytest.mark.parametrize("legacy_top1", [None, {"enabled": False}])
def test_strategy_lab_runtime_context_does_not_depend_on_legacy_top1(
    tmp_path: Path,
    legacy_top1: dict[str, object] | None,
) -> None:
    profile = _profile(tmp_path)
    if legacy_top1 is None:
        profile.pop("strategy_lab_top1")
    else:
        profile["strategy_lab_top1"] = legacy_top1

    context = resolve_strategy_lab_runtime_context(profile, market="hk")

    assert context["artifact_root"] == (
        tmp_path / "runtime/output_shared/research/strategy_lab"
    )
    assert context["config_path"] == tmp_path / "runtime/config.hk.json"


def test_research_corpus_calendar_owner_requires_write_and_closes_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.infrastructure.futu_gateway as futu_gateway

    profile_path = tmp_path / "service.profile.json"
    profile = _profile(tmp_path)
    profile.pop("strategy_lab_top1")
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    command = [
        "research",
        "corpus-calendar",
        "refresh",
        "--market",
        "hk",
        "--profile-path",
        str(profile_path),
        "--coverage-start",
        "2026-08-03",
        "--coverage-end",
        "2026-08-31",
        "--calendar-version",
        "hk-calendar.opend.v1",
    ]

    def explode(**_kwargs: object) -> None:
        raise AssertionError("missing --write must not build an OpenD gateway")

    monkeypatch.setattr(futu_gateway, "build_ready_futu_quote_gateway", explode)
    with pytest.raises(AgentToolError, match="requires --write"):
        handle_research_command(parse_args(command))

    class FakeGateway:
        closed = False

        def get_trading_days_with_receipt(self, **_kwargs: object) -> dict[str, object]:
            return {
                "retcode": 0,
                "rows": [
                    {"time": "2026-08-03", "trade_date_type": "WHOLE"},
                    {"time": "2026-08-04", "trade_date_type": "WHOLE"},
                ],
                "coverage_complete": True,
                "pagination_complete": True,
                "page_count": 1,
            }

        def close(self) -> None:
            self.closed = True

    gateway = FakeGateway()
    import src.application.agent_tool_config as agent_tool_config

    monkeypatch.setattr(
        agent_tool_config,
        "load_runtime_config",
        lambda **_kwargs: (
            tmp_path / "runtime/config.hk.json",
            {
                "_generated": {"market": "hk"},
                "symbols": [
                    {
                        "symbol": "0700.HK",
                        "fetch": {
                            "source": "opend",
                            "host": "127.0.0.1",
                            "port": 11111,
                        },
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(futu_gateway, "build_ready_futu_quote_gateway", lambda **_kwargs: gateway)
    response = handle_research_command(parse_args([*command, "--write"]))

    assert response["ok"] is True
    assert response["tool_name"] == "research.corpus-calendar.refresh"
    assert response["data"]["trading_date_count"] == 2
    assert gateway.closed is True
    assert not (tmp_path / "runtime/output_shared/research/strategy_lab/experiments.sqlite3").exists()
