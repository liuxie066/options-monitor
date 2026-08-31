from __future__ import annotations

import fcntl
import json
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.strategy_lab.readiness import (
    HistoryKReadinessError,
    preview_history_k_readiness,
    read_history_k_readiness_receipt,
    refresh_history_k_readiness,
)
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.strategy_lab_ops import handle_strategy_lab_command
from src.application.tick_cron import tick_cron_is_busy


CONTRACT = "HK.POP260828P145000"
BINDING = {"host": "127.0.0.1", "port": 11111}
OBSERVED = "2026-08-30T03:00:00Z"


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def request_history_kline(self, **kwargs: object) -> dict[str, object]:
        key = kwargs.get("page_req_key")
        self.calls.append(("history", key))
        if key is None:
            return {
                "data": [
                    {"time_key": "2026-08-27 09:31:00", "high": 1.2, "volume": np.int64(10)},
                ],
                "page_req_key": "page-2",
            }
        return {
            "data": [
                {"time_key": "2026-08-27 09:33:00", "high": 1.3, "volume": 0},
            ],
            "page_req_key": None,
        }

    def get_history_kl_quota(self) -> dict[str, object]:
        self.calls.append(("quota", None))
        return {
            "used_quota": 1,
            "remain_quota": 99,
            "detail_list": [
                {"code": "HK.09992", "request_time": "2026-08-30 11:00:00"},
            ],
        }

    def close(self) -> None:
        self.closed = True


def _preview(as_of_utc: str = OBSERVED) -> dict[str, object]:
    return preview_history_k_readiness(
        market="HK",
        account="lx",
        opend_binding=BINDING,
        contract_symbol=CONTRACT,
        underlier_code="HK.09992",
        sample_date="2026-08-27",
        as_of_utc=as_of_utc,
    )


def _refresh(tmp_path: Path, gateway: Any, *, occurred_at_utc: str = OBSERVED) -> dict[str, object]:
    preview = _preview(occurred_at_utc)
    return refresh_history_k_readiness(
        tmp_path / "artifacts",
        gateway=gateway,
        request=preview["probe_request"],
        confirmed_probe_sha256=preview["probe_sha256"],
        actor="operator:lx",
        occurred_at_utc=occurred_at_utc,
        limiter_root=tmp_path / "runtime",
        tick_lock_path=tmp_path / "runtime/locks/tick-hk.lock",
        window_sec=30.0,
        max_calls=10,
    )


def test_preview_is_provider_free_and_refresh_paginates_then_reuses_receipt(
    tmp_path: Path,
) -> None:
    preview = _preview()
    assert preview["status"] == "confirmation_required"
    assert preview["probe_request"]["sample_query"] == {
        "code": CONTRACT,
        "start": "2026-08-27",
        "end": "2026-08-27",
        "ktype": "K_1M",
        "autype": "NONE",
        "fields": ["time_key", "high", "volume"],
        "max_count": 1000,
    }

    gateway = FakeGateway()
    receipt = _refresh(tmp_path, gateway)
    observation = receipt["provider_observation"]
    assert gateway.calls == [("history", None), ("history", "page-2"), ("quota", None)]
    assert observation["readiness_status"] == "ready"
    assert observation["pagination_complete"] is True
    assert observation["sparse_bar_observed"] is True
    assert observation["zero_volume_bar_count"] == 1
    assert observation["no_trade_bar_semantics_observed"] is True
    assert observation["quota"]["security_quota_ceiling"] == 100
    assert observation["quota"]["distinct_security_count"] == 1
    assert observation["quota"]["detail_record_count"] == 1
    assert observation["quota"]["sample_quota_code"] == "HK.09992"

    class ExplodingGateway:
        def request_history_kline(self, **_kwargs: object) -> None:
            raise AssertionError("same-day retry must reuse the immutable receipt")

    repeated = _refresh(tmp_path, ExplodingGateway())
    assert repeated == receipt


def test_probe_rejects_contract_and_quota_code_from_different_securities() -> None:
    with pytest.raises(HistoryKReadinessError) as raised:
        preview_history_k_readiness(
            market="HK",
            account="lx",
            opend_binding=BINDING,
            contract_symbol=CONTRACT,
            underlier_code="HK.00700",
            sample_date="2026-08-27",
            as_of_utc=OBSERVED,
        )
    assert raised.value.reason_code == "history_k_probe_invalid"


def test_running_history_k_probe_does_not_hold_the_tick_lock(
    tmp_path: Path,
) -> None:
    provider_started = threading.Event()
    provider_release = threading.Event()
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    class BlockingGateway(FakeGateway):
        def request_history_kline(self, **kwargs: object) -> dict[str, object]:
            if kwargs.get("page_req_key") is None:
                provider_started.set()
                assert provider_release.wait(timeout=2.0)
            return super().request_history_kline(**kwargs)

    def run_probe() -> None:
        try:
            results.append(_refresh(tmp_path, BlockingGateway()))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=run_probe)
    thread.start()
    assert provider_started.wait(timeout=1.0)
    try:
        assert tick_cron_is_busy(tmp_path / "runtime/locks/tick-hk.lock") is False
    finally:
        provider_release.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert errors == []
    assert results[0]["provider_observation"]["readiness_status"] == "ready"


def test_refresh_fails_closed_for_confirmation_tick_and_protection_window(
    tmp_path: Path,
) -> None:
    preview = _preview()
    with pytest.raises(HistoryKReadinessError) as mismatch:
        refresh_history_k_readiness(
            tmp_path / "artifacts",
            gateway=FakeGateway(),
            request=preview["probe_request"],
            confirmed_probe_sha256="a" * 64,
            actor="operator:lx",
            occurred_at_utc=OBSERVED,
            limiter_root=tmp_path / "runtime",
            tick_lock_path=tmp_path / "runtime/locks/tick-hk.lock",
            window_sec=30.0,
            max_calls=10,
        )
    assert mismatch.value.reason_code == "history_k_probe_confirmation_mismatch"

    tick_lock = tmp_path / "runtime/locks/tick-hk.lock"
    tick_lock.parent.mkdir(parents=True)
    gateway = FakeGateway()
    with tick_lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with pytest.raises(HistoryKReadinessError) as busy:
            _refresh(tmp_path, gateway)
    assert busy.value.reason_code == "tick_busy"
    assert gateway.calls == []

    protected_time = "2026-08-31T08:00:00Z"
    gateway = FakeGateway()
    with pytest.raises(HistoryKReadinessError) as protected:
        _refresh(tmp_path, gateway, occurred_at_utc=protected_time)
    assert protected.value.reason_code == "tick_protection_window"
    assert gateway.calls == []


def test_receipt_read_fails_closed_for_expiry_and_tamper(tmp_path: Path) -> None:
    receipt = _refresh(tmp_path, FakeGateway())
    with pytest.raises(HistoryKReadinessError) as wrong_endpoint:
        read_history_k_readiness_receipt(
            tmp_path / "artifacts",
            probe_sha256=receipt["probe_sha256"],
            expected_opend_binding={"host": "127.0.0.1", "port": 22222},
            as_of_utc=OBSERVED,
        )
    assert wrong_endpoint.value.reason_code == "history_k_readiness_invalid"

    with pytest.raises(HistoryKReadinessError) as expired:
        read_history_k_readiness_receipt(
            tmp_path / "artifacts",
            probe_sha256=receipt["probe_sha256"],
            expected_opend_binding=BINDING,
            as_of_utc="2026-09-01T03:00:00Z",
        )
    assert expired.value.reason_code == "history_k_readiness_expired"

    path = tmp_path / "artifacts" / str(receipt["receipt_ref"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["provider_observation"]["row_count"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HistoryKReadinessError) as tampered:
        read_history_k_readiness_receipt(
            tmp_path / "artifacts",
            probe_sha256=receipt["probe_sha256"],
            expected_opend_binding=BINDING,
            as_of_utc=OBSERVED,
        )
    assert tampered.value.reason_code == "history_k_readiness_invalid"


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


def _patch_context_owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.strategy_lab.service as service

    monkeypatch.setattr(
        service,
        "load_runtime_config",
        lambda **_kwargs: (
            tmp_path / "runtime/config.hk.json",
            {"accounts": ["lx"]},
        ),
    )
    monkeypatch.setattr(
        service,
        "infer_futu_portfolio_settings",
        lambda _config, *, account: dict(BINDING),
    )
    monkeypatch.setattr(
        service,
        "resolve_position_ledger_sqlite_path",
        lambda **_kwargs: tmp_path / "runtime/option-positions.sqlite3",
    )


def test_public_cli_previews_without_provider_then_refreshes_confirmed_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    _patch_context_owners(tmp_path, monkeypatch)

    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(_profile(tmp_path)), encoding="utf-8")
    command = [
        "strategy-lab",
        "readiness",
        "refresh-history-k",
        "--profile-path",
        str(profile_path),
        "--contract-symbol",
        CONTRACT,
        "--underlier-code",
        "HK.09992",
        "--sample-date",
        "2026-08-27",
    ]
    monkeypatch.setattr(cli, "_now_utc", lambda: OBSERVED)

    def explode(**_kwargs: object) -> None:
        raise AssertionError("preview must not build an OpenD gateway")

    monkeypatch.setattr(cli, "build_futu_gateway", explode)
    preview_response = handle_strategy_lab_command(parse_args(command))
    probe_hash = preview_response["data"]["probe_sha256"]
    assert preview_response["ok"] is True

    with pytest.raises(AgentToolError, match="requires confirmed probe hash"):
        handle_strategy_lab_command(parse_args([*command, "--write"]))

    gateway = FakeGateway()
    monkeypatch.setattr(cli, "build_futu_gateway", lambda **_kwargs: gateway)
    monkeypatch.setattr(
        cli,
        "load_runtime_config",
        lambda **_kwargs: (
            tmp_path / "runtime/config.hk.json",
            {
                "runtime": {
                    "opend_rate_limits": {
                        "history_kline": {"window_sec": 30, "max_calls": 10, "max_wait_sec": 30},
                    }
                }
            },
        ),
    )
    response = handle_strategy_lab_command(
        parse_args(
            [
                *command,
                "--confirmed-probe-sha256",
                probe_hash,
                "--actor",
                "operator:lx",
                "--write",
            ]
        )
    )
    assert response["ok"] is True
    assert response["data"]["status"] == "ready"
    assert gateway.closed is True


def test_public_cli_checks_tick_before_building_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.interfaces.cli.strategy_lab_ops as cli

    _patch_context_owners(tmp_path, monkeypatch)

    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(_profile(tmp_path)), encoding="utf-8")
    command = [
        "strategy-lab",
        "readiness",
        "refresh-history-k",
        "--profile-path",
        str(profile_path),
        "--contract-symbol",
        CONTRACT,
        "--underlier-code",
        "HK.09992",
        "--sample-date",
        "2026-08-27",
    ]
    monkeypatch.setattr(cli, "_now_utc", lambda: OBSERVED)
    preview = handle_strategy_lab_command(parse_args(command))["data"]
    monkeypatch.setattr(
        cli,
        "load_runtime_config",
        lambda **_kwargs: (
            tmp_path / "runtime/config.hk.json",
            {
                "runtime": {
                    "opend_rate_limits": {
                        "history_kline": {
                            "window_sec": 30,
                            "max_calls": 10,
                            "max_wait_sec": 30,
                        },
                    }
                }
            },
        ),
    )

    def explode(**_kwargs: object) -> None:
        raise AssertionError("busy Tick must be checked before OpenD startup")

    monkeypatch.setattr(cli, "build_futu_gateway", explode)
    tick_lock = tmp_path / "runtime/locks/tick-hk.lock"
    tick_lock.parent.mkdir(parents=True)
    with tick_lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with pytest.raises(AgentToolError, match="HK Tick is running"):
            handle_strategy_lab_command(
                parse_args(
                    [
                        *command,
                        "--confirmed-probe-sha256",
                        str(preview["probe_sha256"]),
                        "--actor",
                        "operator:lx",
                        "--write",
                    ]
                )
            )
