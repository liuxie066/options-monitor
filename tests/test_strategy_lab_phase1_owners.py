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
    }


def _futu_config(*, market: str = "hk", host: str = "127.0.0.1", port: int = 11111) -> dict[str, object]:
    return {
        "_generated": {"market": market},
        "accounts": ["lx"],
        "symbols": [
            {
                "symbol": "0700.HK" if market == "hk" else "NVDA",
                "fetch": {"source": "opend", "host": host, "port": port},
            }
        ],
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.service as service

    profile = _profile(tmp_path)
    monkeypatch.setattr(
        service,
        "load_runtime_config",
        lambda **_kwargs: (Path(profile["config_paths"]["hk"]), _futu_config()),
    )
    monkeypatch.setattr(
        service,
        "infer_futu_portfolio_settings",
        lambda _config, *, account: {"host": "127.0.0.1", "port": 11111},
    )
    ledger_calls: list[dict[str, object]] = []

    def resolve_ledger(**kwargs: object) -> Path:
        ledger_calls.append(kwargs)
        return tmp_path / "runtime/option-positions.sqlite3"

    monkeypatch.setattr(service, "resolve_position_ledger_sqlite_path", resolve_ledger)
    context = resolve_strategy_lab_context(profile)

    assert context["artifact_root"] == (tmp_path / "runtime/output_shared/research/strategy_lab")
    assert context["store_path"] == context["artifact_root"] / "experiments.sqlite3"
    assert (context["market"], context["account"]) == ("hk", "lx")
    assert context["opend_binding"] == {"host": "127.0.0.1", "port": 11111}
    assert context["opend_limiter_root"] == tmp_path / "runtime"
    assert context["tick_markets"] == ("hk",)
    assert context["tick_lock_paths"] == (tmp_path / "runtime/locks/tick-hk.lock",)
    assert ledger_calls[0]["runtime_root"] == tmp_path / "runtime"

    with pytest.raises(StrategyLabContextError, match="absolute path"):
        resolve_strategy_lab_context({**profile, "runtime_root": "runtime"})


def test_strategy_lab_context_binds_ledger_to_profile_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.service as service

    profile = _profile(tmp_path)
    profile_runtime = tmp_path / "profile-runtime"
    config_path = tmp_path / "runtime-config" / "config.hk.json"
    data_config = tmp_path / "portfolio-config" / "portfolio.runtime.json"
    profile.update(
        {
            "runtime_root": str(profile_runtime),
            "config_paths": {"hk": str(config_path)},
        }
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "environment-runtime"))
    monkeypatch.setenv("OM_DATA_CONFIG", str(tmp_path / "environment-data.json"))
    monkeypatch.setattr(
        service,
        "load_runtime_config",
        lambda **_kwargs: (
            config_path,
            {**_futu_config(), "portfolio": {"data_config": str(data_config)}},
        ),
    )
    monkeypatch.setattr(
        service,
        "infer_futu_portfolio_settings",
        lambda _config, *, account: {"host": "127.0.0.1", "port": 11111},
    )

    context = resolve_strategy_lab_context(profile)

    assert context["ledger_path"] == (profile_runtime / "output_shared/state/option_positions.sqlite3").resolve()


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"accounts": ["sy"]},
        {
            "accounts": ["lx"],
            "account_settings": {"lx": {"type": "external_holdings"}},
        },
    ],
)
def test_strategy_lab_context_requires_configured_futu_lx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> None:
    import src.application.strategy_lab.service as service

    profile = _profile(tmp_path)
    monkeypatch.setattr(
        service,
        "load_runtime_config",
        lambda **_kwargs: (Path(profile["config_paths"]["hk"]), config),
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid lx config must fail before resolving owners")

    monkeypatch.setattr(service, "infer_futu_portfolio_settings", unexpected)
    monkeypatch.setattr(service, "resolve_position_ledger_sqlite_path", unexpected)

    with pytest.raises(StrategyLabContextError):
        resolve_strategy_lab_context(profile)


def test_strategy_lab_runtime_context_uses_ordinary_profile(tmp_path: Path) -> None:
    profile = _profile(tmp_path)

    context = resolve_strategy_lab_runtime_context(profile, market="hk")

    assert context["artifact_root"] == (tmp_path / "runtime/output_shared/research/strategy_lab")
    assert context["store_path"] == context["artifact_root"] / "experiments.sqlite3"
    assert context["config_path"] == tmp_path / "runtime/config.hk.json"
    assert not context["config_path"].exists()


def test_strategy_lab_context_binds_every_market_sharing_the_opend_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.service as service

    profile = _profile(tmp_path)
    profile["markets"] = ["hk", "us"]
    profile["config_paths"] = {
        "hk": str(tmp_path / "runtime/config.hk.json"),
        "us": str(tmp_path / "runtime/config.us.json"),
    }
    monkeypatch.setattr(
        service,
        "load_runtime_config",
        lambda *, expected_market, **_kwargs: (
            Path(profile["config_paths"][expected_market]),
            _futu_config(market=expected_market),
        ),
    )
    monkeypatch.setattr(
        service,
        "infer_futu_portfolio_settings",
        lambda _config, *, account: {"host": "127.0.0.1", "port": 11111},
    )

    context = resolve_strategy_lab_context(profile)

    assert context["tick_markets"] == ("hk", "us")
    assert context["tick_lock_paths"] == (
        tmp_path / "runtime/locks/tick-hk.lock",
        tmp_path / "runtime/locks/tick-us.lock",
    )


@pytest.mark.parametrize(
    ("us_host", "portfolio_host"),
    [("other", "127.0.0.1"), ("127.0.0.1", "portfolio")],
)
def test_strategy_lab_context_rejects_split_or_mismatched_opend_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    us_host: str,
    portfolio_host: str,
) -> None:
    import src.application.strategy_lab.service as service

    profile = _profile(tmp_path)
    profile["markets"] = ["hk", "us"]
    profile["config_paths"] = {
        "hk": str(tmp_path / "runtime/config.hk.json"),
        "us": str(tmp_path / "runtime/config.us.json"),
    }
    monkeypatch.setattr(
        service,
        "load_runtime_config",
        lambda *, expected_market, **_kwargs: (
            Path(profile["config_paths"][expected_market]),
            _futu_config(
                market=expected_market,
                host=us_host if expected_market == "us" else "127.0.0.1",
            ),
        ),
    )
    monkeypatch.setattr(
        service,
        "infer_futu_portfolio_settings",
        lambda _config, *, account: {"host": portfolio_host, "port": 11111},
    )

    with pytest.raises(StrategyLabContextError):
        resolve_strategy_lab_context(profile)


def test_research_corpus_calendar_owner_requires_write_and_closes_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.infrastructure.futu_gateway as futu_gateway

    profile_path = tmp_path / "service.profile.json"
    profile = _profile(tmp_path)
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
