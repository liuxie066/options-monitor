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
        "schema_state": {"status": "ready", "schema_version": 3},
        "feature_status": {
            "maintainer_available": True,
            "user_opt_in": True,
            "effective": True,
        },
        "corpus_status": {"days_total": 1},
        "calendar_binding": {
            "market": "HK",
            "market_calendar_version": "hk-calendar.v1",
            "coverage_start": "2026-08-01",
            "coverage_end": "2026-12-31",
            "trading_dates": ["2026-08-01", "2026-12-31"],
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

    for invalid_calendar in ({}, {"market": "HK"}, {**common["calendar_binding"], "trading_dates": []}):
        blocked_calendar = build_top1_readiness(
            **{**common, "calendar_binding": invalid_calendar},
            capability_facts={name: True for name in CAPABILITY_FACTS},
        )
        assert blocked_calendar["validation_runtime_ready"] is False
        assert "market_calendar_binding_unavailable" in blocked_calendar[
            "validation_runtime_blockers"
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
