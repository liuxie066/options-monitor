from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.strategy_lab.top1.readiness import (
    ADVANCE_SERVICE,
    ADVANCE_TIMER,
    CAPABILITY_FACTS,
    build_top1_readiness,
)


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
        "services": [{"name": ADVANCE_SERVICE}, {"name": ADVANCE_TIMER}],
        "strategy_lab_top1": {
            "enabled": True,
            "market": "hk",
            "account": "lx",
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
            "advance_interval": 300,
            "timeout_start_sec": 120,
        },
    }


def _source_facts() -> tuple[dict[str, object], dict[str, object]]:
    drift = {
        "summary": {"status": "ok"},
        "expected_services": [ADVANCE_SERVICE, ADVANCE_TIMER],
        "profile_services": [ADVANCE_SERVICE, ADVANCE_TIMER],
        "installed_units": [ADVANCE_SERVICE, ADVANCE_TIMER],
    }
    status = {
        "services": [
            {
                "name": ADVANCE_TIMER,
                "active": {"status": "ok"},
                "enabled": {"status": "ok"},
            }
        ]
    }
    return drift, status


def test_readiness_requires_every_live_capability_fact(tmp_path: Path) -> None:
    drift, status = _source_facts()
    common = {
        "profile": _profile(tmp_path),
        "drift": drift,
        "service_status": status,
        "schema_state": {"status": "ready", "schema_version": 4},
        "corpus_status": {"days_total": 1},
        "calendar_binding": {
            "market": "HK",
            "market_calendar_version": "hk-calendar.v1",
            "coverage_start": "2026-08-01",
            "coverage_end": "2026-12-31",
            "trading_dates": ["2026-08-01", "2026-12-31"],
            "trading_sessions": [
                {"trading_date": "2026-08-01", "trade_date_type": "WHOLE"},
                {"trading_date": "2026-12-31", "trade_date_type": "WHOLE"},
            ],
            "snapshot_ref": "calendar.json",
            "snapshot_content_sha256": "a" * 64,
            "snapshot_file_sha256": "c" * 64,
            "source_receipt_sha256": "b" * 64,
        },
    }

    blocked = build_top1_readiness(**common)
    assert blocked["source_delivery_ready"] is True
    assert blocked["validation_runtime_ready"] is False
    assert {
        f"{name}_missing" for name in CAPABILITY_FACTS
    }.issubset(blocked["validation_runtime_blockers"])

    ready = build_top1_readiness(
        **common, capability_facts={name: True for name in CAPABILITY_FACTS}
    )
    assert ready["source_delivery_ready"] is True
    assert ready["validation_runtime_ready"] is True
    assert ready["validation_runtime_blockers"] == []

    missing_corpus = build_top1_readiness(
        **{**common, "corpus_status": None},
        capability_facts={name: True for name in CAPABILITY_FACTS},
    )
    assert missing_corpus["validation_runtime_ready"] is False
    assert "strategy_lab_top1_corpus_unavailable" in missing_corpus[
        "validation_runtime_blockers"
    ]

    malformed_status = build_top1_readiness(
        **{**common, "service_status": {"services": None}},
        capability_facts={name: True for name in CAPABILITY_FACTS},
    )
    assert malformed_status["source_delivery_ready"] is False
    assert malformed_status["facts"]["timer_status"] is None

    relative_env_profile = _profile(tmp_path)
    relative_env_profile["env_file"] = "relative.env"
    relative_env = build_top1_readiness(
        **{**common, "profile": relative_env_profile},
        capability_facts={name: True for name in CAPABILITY_FACTS},
    )
    assert relative_env["source_delivery_ready"] is False
    assert "strategy_lab_top1_env_file_missing" in relative_env[
        "source_delivery_blockers"
    ]

    for invalid_calendar in (
        {},
        {"market": "HK"},
        {**common["calendar_binding"], "trading_dates": []},
        {
            **common["calendar_binding"],
            "trading_sessions": [
                {"trading_date": "2026-08-01", "trade_date_type": "UNKNOWN"},
                {"trading_date": "2026-12-31", "trade_date_type": "WHOLE"},
            ],
        },
    ):
        blocked_calendar = build_top1_readiness(
            **{**common, "calendar_binding": invalid_calendar},
            capability_facts={name: True for name in CAPABILITY_FACTS},
        )
        assert blocked_calendar["validation_runtime_ready"] is False
        assert "market_calendar_binding_unavailable" in blocked_calendar[
            "validation_runtime_blockers"
        ]


def test_readiness_ignores_unrelated_service_activation_drift(
    tmp_path: Path,
) -> None:
    drift, status = _source_facts()
    drift.update(
        {
            "summary": {"status": "error"},
            "missing_profile_units": [],
            "missing_installed_units": [],
            "mismatched_units": [],
            "activation_drift_units": [
                "options-monitor-strategy-lab-sample.timer"
            ],
            "activation_preservation_conflicts": [],
            "execution_drift_units": [],
        }
    )
    result = build_top1_readiness(
        profile=_profile(tmp_path),
        drift=drift,
        service_status=status,
        schema_state={"status": "ready", "schema_version": 4},
        corpus_status={"days_total": 1},
        calendar_binding=None,
    )

    assert result["source_delivery_ready"] is True
    assert "strategy_lab_top1_service_drift" not in result[
        "source_delivery_blockers"
    ]

    drift["activation_drift_units"] = [ADVANCE_TIMER]
    blocked = build_top1_readiness(
        profile=_profile(tmp_path),
        drift=drift,
        service_status=status,
        schema_state={"status": "ready", "schema_version": 4},
        corpus_status={"days_total": 1},
        calendar_binding=None,
    )
    assert "strategy_lab_top1_service_drift" in blocked[
        "source_delivery_blockers"
    ]

    unavailable_drift, _status = _source_facts()
    unavailable_drift["summary"] = {"status": "error"}
    unavailable = build_top1_readiness(
        profile=_profile(tmp_path),
        drift=unavailable_drift,
        service_status=status,
        schema_state={"status": "ready", "schema_version": 4},
        corpus_status={"days_total": 1},
        calendar_binding=None,
    )
    assert "strategy_lab_top1_service_drift" in unavailable[
        "source_delivery_blockers"
    ]


def test_readiness_cli_is_read_only_and_reports_uninitialized_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.interfaces.cli.strategy_lab_top1 as cli
    from src.interfaces.cli.main import parse_args

    profile = _profile(tmp_path)
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    drift, status = _source_facts()
    monkeypatch.setattr(cli, "service_drift", lambda **_kwargs: drift)
    monkeypatch.setattr(
        cli, "service_status_from_profile", lambda *_args, **_kwargs: status
    )
    monkeypatch.setattr(
        cli,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("missing")),
    )
    args = parse_args(
        [
            "research",
            "strategy-lab",
            "top1-loop",
            "readiness",
            "--market",
            "hk",
            "--account",
            "lx",
            "--profile-path",
            str(profile_path),
        ]
    )

    response = cli.handle_top1_command(args)

    store_path = (
        tmp_path
        / "runtime"
        / "output_shared"
        / "research"
        / "strategy_lab"
        / "experiments.sqlite3"
    )
    assert response["ok"] is True
    assert response["data"]["validation_runtime_ready"] is False
    assert response["data"]["facts"]["store_schema"]["status"] == "not_initialized"
    assert not store_path.exists()


def test_research_preview_cli_is_read_only_and_start_requires_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.interfaces.cli.strategy_lab_top1 as cli
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli.main import parse_args

    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(_profile(tmp_path)), encoding="utf-8")
    preview = {
        "status": "blocked",
        "reason_codes": ["research_window_coverage_missing"],
    }
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(cli, "_research_inputs", lambda *_args: ({"scope": "fixed"}, {}))
    monkeypatch.setattr(
        cli,
        "preview_sell_put_top1_research",
        lambda _store, root, **kwargs: calls.append((root, kwargs)) or preview,
    )
    common = [
        "research",
        "strategy-lab",
        "top1-loop",
        "research",
        "preview",
        "--market",
        "hk",
        "--account",
        "lx",
        "--profile-path",
        str(profile_path),
        "--cutoff-at-utc",
        "2026-08-15T00:00:00Z",
        "--latest-mature-trading-date",
        "2026-08-14",
    ]

    response = cli.handle_top1_command(parse_args(common))

    assert response["ok"] is True
    assert response["data"] == preview
    assert calls[0][1] == {"scope": "fixed"}
    assert not (
        tmp_path / "runtime/output_shared/research/strategy_lab/experiments.sqlite3"
    ).exists()

    monkeypatch.setattr(
        cli,
        "_research_inputs",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not read inputs")),
    )
    start = list(common)
    start[4] = "start"
    start.extend(["--confirmed-start-file", str(tmp_path / "command.json")])
    with pytest.raises(AgentToolError, match="requires --write"):
        cli.handle_top1_command(parse_args(start))


def test_validation_and_receipt_cli_expose_the_remaining_mvp_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.interfaces.cli.strategy_lab_top1 as cli
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli.main import parse_args

    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(_profile(tmp_path)), encoding="utf-8")

    class ReadyStore:
        def schema_state(self) -> dict[str, str]:
            return {"status": "ready"}

    store = ReadyStore()
    monkeypatch.setattr(cli, "ExperimentStore", lambda _path: store)
    monkeypatch.setattr(
        cli,
        "load_runtime_config",
        lambda **_kwargs: (
            tmp_path / "config.hk.json",
            {"schedule": {"timezone": "Asia/Hong_Kong"}},
        ),
    )
    monkeypatch.setattr(
        cli,
        "preview_sell_put_top1_validation",
        lambda received_store, _root, **_kwargs: {
            "status": "blocked",
            "reason_codes": [
                "research_leader_unavailable"
                if received_store is store
                else "wrong_store"
            ],
        },
    )
    common = [
        "research",
        "strategy-lab",
        "top1-loop",
        "validation",
        "preview",
        "--market",
        "hk",
        "--account",
        "lx",
        "--profile-path",
        str(profile_path),
        "--experiment-id",
        "experiment-001",
        "--validation-start-trading-date",
        "2026-08-17",
    ]

    response = cli.handle_top1_command(parse_args(common))

    assert response["ok"] is True
    assert response["data"]["reason_codes"] == ["research_leader_unavailable"]
    start = list(common)
    start[4] = "start"
    start.extend(["--confirmed-start-file", str(tmp_path / "command.json")])
    with pytest.raises(AgentToolError, match="requires --write"):
        cli.handle_top1_command(parse_args(start))

    monkeypatch.setattr(
        cli,
        "read_public_receipt",
        lambda received_store, *, experiment_id: {
            "experiment_id": experiment_id,
            "same_store": received_store is store,
        },
    )
    receipt = cli.handle_top1_command(
        parse_args(
            [
                "research",
                "strategy-lab",
                "top1-loop",
                "receipt",
                "--market",
                "hk",
                "--account",
                "lx",
                "--profile-path",
                str(profile_path),
                "--experiment-id",
                "experiment-001",
            ]
        )
    )
    assert receipt["data"] == {
        "experiment_id": "experiment-001",
        "same_store": True,
    }


def test_calendar_refresh_cli_requires_write_and_closes_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.interfaces.cli.strategy_lab_top1 as cli
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli.main import parse_args

    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(_profile(tmp_path)), encoding="utf-8")
    command = [
        "research",
        "strategy-lab",
        "top1-loop",
        "calendar",
        "refresh",
        "--market",
        "hk",
        "--account",
        "lx",
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

    monkeypatch.setattr(cli, "build_ready_futu_quote_gateway", explode)
    with pytest.raises(AgentToolError, match="requires --write"):
        cli.handle_top1_command(parse_args(command))

    class FakeGateway:
        def __init__(self) -> None:
            self.closed = False

        def get_trading_days_with_receipt(
            self, **_kwargs: object
        ) -> dict[str, object]:
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
    monkeypatch.setattr(
        cli, "build_ready_futu_quote_gateway", lambda **_kwargs: gateway
    )
    response = cli.handle_top1_command(parse_args([*command, "--write"]))

    assert response["ok"] is True
    assert response["data"]["status"] == "published"
    assert response["data"]["trading_date_count"] == 2
    assert "trading_dates" not in response["data"]
    assert gateway.closed is True
    assert not (
        tmp_path
        / "runtime/output_shared/research/strategy_lab/experiments.sqlite3"
    ).exists()


def test_capability_refresh_cli_requires_write_and_readiness_only_reads_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.interfaces.cli.strategy_lab_top1 as cli
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.strategy_lab.top1.capability_receipts import (
        ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
    )
    from src.interfaces.cli.main import parse_args

    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(_profile(tmp_path)), encoding="utf-8")
    fee_plan_path = tmp_path / "fee-plan.json"
    fee_plan_path.write_text(
        json.dumps(
            {
                "schema_version": ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
                "market": "HK",
                "account": "lx",
                "commission_free": True,
                "platform_fee": 15.0,
                "fee_plan_ref": "futu-hk-plan.v1",
                "observed_at_utc": "2026-08-16T01:00:00Z",
                "evidence_ref": "operator://futu/lx/fee-plan/2026-08-16",
                "evidence_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    command = [
        "research",
        "strategy-lab",
        "top1-loop",
        "capabilities",
        "refresh",
        "--market",
        "hk",
        "--account",
        "lx",
        "--profile-path",
        str(profile_path),
        "--fee-plan-receipt-path",
        str(fee_plan_path),
        "--stock-owner",
        "HK.00700",
        "--contract-symbol",
        "HK.00700260828P00400000",
        "--terms-expiration",
        "2026-08-28",
        "--close-expiration",
        "2026-08-14",
    ]

    def explode(**_kwargs: object) -> None:
        raise AssertionError("missing --write or readiness must not build a gateway")

    monkeypatch.setattr(cli, "build_ready_futu_quote_gateway", explode)
    with pytest.raises(AgentToolError, match="requires --write"):
        cli.handle_top1_command(parse_args(command))

    fee_plan_link = tmp_path / "fee-plan-link.json"
    fee_plan_link.symlink_to(fee_plan_path)
    linked_command = list(command)
    linked_command[linked_command.index("--fee-plan-receipt-path") + 1] = str(
        fee_plan_link
    )
    with pytest.raises(AgentToolError, match="cannot be read"):
        cli.handle_top1_command(parse_args([*linked_command, "--write"]))

    class FakeGateway:
        closed = False

        def get_snapshot(self, codes: list[str]) -> list[dict[str, object]]:
            return [{"code": codes[0], "bid_price": 1.2, "ask_price": 1.3}]

        def get_exact_expiration_option_terms(
            self, **_kwargs: object
        ) -> dict[str, object]:
            return {
                "contract_symbol": "HK.00700260828P00400000",
                "stock_owner": "HK.00700",
                "expiration": "2026-08-28",
                "option_type": "PUT",
                "option_standard_type": "STANDARD",
                "strike": 400.0,
                "multiplier": 100,
                "currency": "HKD",
            }

        def get_history_kl_quota(self) -> dict[str, object]:
            return {"used_quota": 0, "remain_quota": 100, "detail_list": []}

        def get_exact_expiration_close(self, **_kwargs: object) -> dict[str, object]:
            return {
                "code": "HK.00700",
                "expiration": "2026-08-14",
                "close": 600.0,
            }

        def close(self) -> None:
            self.closed = True

    gateway = FakeGateway()
    monkeypatch.setattr(
        cli, "build_ready_futu_quote_gateway", lambda **_kwargs: gateway
    )
    refreshed = cli.handle_top1_command(parse_args([*command, "--write"]))
    assert refreshed["ok"] is True
    assert refreshed["data"]["capabilities"] == {
        name: True for name in CAPABILITY_FACTS
    }
    assert gateway.closed is True

    drift, status = _source_facts()
    monkeypatch.setattr(cli, "service_drift", lambda **_kwargs: drift)
    monkeypatch.setattr(
        cli, "service_status_from_profile", lambda *_args, **_kwargs: status
    )
    monkeypatch.setattr(
        cli,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("missing")),
    )
    monkeypatch.setattr(cli, "build_ready_futu_quote_gateway", explode)
    readiness = cli.handle_top1_command(
        parse_args(
            [
                "research",
                "strategy-lab",
                "top1-loop",
                "readiness",
                "--market",
                "hk",
                "--account",
                "lx",
                "--profile-path",
                str(profile_path),
            ]
        )
    )
    assert readiness["data"]["facts"]["capabilities"] == {
        name: True for name in CAPABILITY_FACTS
    }
    assert readiness["data"]["facts"]["capability_receipt"] is not None


def test_disabled_advance_migrates_store_but_loads_no_runtime_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.interfaces.cli.strategy_lab_top1 as cli
    from src.interfaces.cli.main import parse_args

    profile = _profile(tmp_path)
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.delenv("OM_STRATEGY_LAB_TOP1_AVAILABLE", raising=False)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("disabled advance must not load runtime dependencies")

    monkeypatch.setattr(cli, "load_runtime_config", explode)
    monkeypatch.setattr(cli, "service_drift", explode)
    monkeypatch.setattr(cli, "service_status_from_profile", explode)
    monkeypatch.setattr(cli, "build_ready_futu_quote_gateway", explode)
    args = parse_args(
        [
            "research",
            "strategy-lab",
            "top1-loop",
            "advance",
            "--scheduled",
            "--market",
            "hk",
            "--account",
            "lx",
            "--profile-path",
            str(profile_path),
            "--write",
        ]
    )

    response = cli.handle_top1_command(args)

    store_path = (
        tmp_path
        / "runtime"
        / "output_shared"
        / "research"
        / "strategy_lab"
        / "experiments.sqlite3"
    )
    assert response["ok"] is True
    assert response["data"]["status"] == "disabled"
    assert store_path.exists()


def test_advance_rejects_profile_mismatch_and_missing_write_before_migration(
    tmp_path: Path,
) -> None:
    import src.interfaces.cli.strategy_lab_top1 as cli
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli.main import parse_args

    profile = _profile(tmp_path)
    profile_path = tmp_path / "service.profile.json"
    top1 = dict(profile["strategy_lab_top1"])
    top1["account"] = "sy"
    profile["strategy_lab_top1"] = top1
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    command = [
        "research",
        "strategy-lab",
        "top1-loop",
        "advance",
        "--scheduled",
        "--market",
        "hk",
        "--account",
        "lx",
        "--profile-path",
        str(profile_path),
    ]
    store_path = (
        tmp_path
        / "runtime"
        / "output_shared"
        / "research"
        / "strategy_lab"
        / "experiments.sqlite3"
    )

    with pytest.raises(AgentToolError, match="disagrees"):
        cli.handle_top1_command(parse_args([*command, "--write"]))
    assert not store_path.exists()

    non_systemd = _profile(tmp_path)
    non_systemd["service_provider"] = "launchd"
    profile_path.write_text(json.dumps(non_systemd), encoding="utf-8")
    with pytest.raises(AgentToolError, match="systemd profile"):
        cli.handle_top1_command(parse_args([*command, "--write"]))
    assert not store_path.exists()

    profile_path.write_text(json.dumps(_profile(tmp_path)), encoding="utf-8")
    with pytest.raises(AgentToolError, match="requires --scheduled and --write"):
        cli.handle_top1_command(parse_args(command))
    assert not store_path.exists()
