from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.application.multi_tick import required_data_prefetch as prefetch
from src.infrastructure.futu_gateway_pool import (
    ThreadLocalFutuGatewayPool,
    is_gateway_connection_error,
)


class _Gateway:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _connection_failure_payload(
    *, error_field: str = "snapshot_errors"
) -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "rows": [],
        "meta": {
            "status": "error",
            "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
            "error": "required option snapshots are incomplete",
            error_field: [
                {
                    "stage": "market_snapshot",
                    "error_code": "TRANSIENT",
                    "message": "connection reset by peer",
                },
                {
                    "stage": "market_snapshot_completeness",
                    "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
                    "message": "missing one requested snapshot code",
                },
            ],
        },
    }


def _coverage_failure_payload() -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "rows": [],
        "meta": {
            "status": "error",
            "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
            "error": "required option snapshots are incomplete",
            "errors": [
                {
                    "stage": "market_snapshot_completeness",
                    "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
                    "message": "missing one requested snapshot code",
                }
            ],
            "snapshot_errors": [],
            "spot_errors": [],
        },
    }


def _opend_fetch_config() -> dict[str, Any]:
    endpoint = {"max_wait_sec": 1.0, "window_sec": 1.0, "max_calls": 1}
    return {
        "option_chain": dict(endpoint),
        "market_snapshot": dict(endpoint),
        "option_expiration": dict(endpoint),
    }


def _batch_config() -> SimpleNamespace:
    return SimpleNamespace(
        market_snapshot=200,
        market_snapshot_fallback_max_codes=100,
        market_snapshot_fallback_batch_size=20,
    )


def _patch_source_snapshot_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prefetch,
        "adapt_opend_tool_payload",
        lambda payload: {"payload": payload},
    )
    monkeypatch.setattr(
        prefetch.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )


@pytest.mark.parametrize(
    "error_field",
    ["errors", "snapshot_errors", "spot_errors"],
)
def test_nested_provider_connection_evidence_survives_coverage_rewrite(
    error_field: str,
) -> None:
    payload = _connection_failure_payload(error_field=error_field)

    assert is_gateway_connection_error(payload) is True


def test_pure_coverage_contract_and_artifact_failures_do_not_close_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _Gateway()
    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: gateway,
    )
    pool = ThreadLocalFutuGatewayPool()
    pool.get_gateway(host="127.0.0.1", port=11111, chain_cache=True)

    pool.mark_failure(_coverage_failure_payload())
    pool.mark_failure(RuntimeError("required-data contract is incomplete"))
    pool.mark_failure(OSError("artifact write failed"))

    assert gateway.close_calls == 0
    assert is_gateway_connection_error(_coverage_failure_payload()) is False


def test_inprocess_structured_connection_failures_close_and_rebuild_gateway(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[_Gateway] = []
    fetched_with: list[_Gateway] = []

    def _build_gateway(**kwargs: Any) -> _Gateway:
        gateway = _Gateway()
        built.append(gateway)
        return gateway

    def _fetch_symbol(symbol: str, **kwargs: Any) -> dict[str, Any]:
        fetched_with.append(kwargs["gateway"])
        return _connection_failure_payload()

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        _build_gateway,
    )
    pool = ThreadLocalFutuGatewayPool()
    monkeypatch.setattr(prefetch, "_gateway_pool", pool)
    monkeypatch.setattr(prefetch, "fetch_symbol", _fetch_symbol)
    _patch_source_snapshot_writes(monkeypatch)

    symbol_cfg = {
        "symbol": "AAPL",
        "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
    }
    for _ in range(2):
        result = prefetch._fetch_one_inprocess(
            symbol_cfg,
            base=tmp_path,
            shared_required=tmp_path / "required_data",
            opend_fetch_cfg=_opend_fetch_config(),
            batch_cfg=_batch_config(),
        )
        assert result["ok"] is False

    assert len(built) == 1
    assert fetched_with == [built[0], built[0]]
    assert built[0].close_calls == 1

    replacement = pool.get_gateway(
        host="127.0.0.1",
        port=11111,
        chain_cache=True,
    )
    assert replacement is built[1]


def test_artifact_failure_follows_typed_provider_success_without_health_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _RecordingPool:
        def get_gateway(self, **kwargs: Any) -> _Gateway:
            events.append("get_gateway")
            return _Gateway()

        def mark_success(self) -> None:
            events.append("mark_success")

        def mark_failure(self, failure: Any) -> None:
            events.append("mark_failure")

    def _fetch_symbol(symbol: str, **kwargs: Any) -> dict[str, Any]:
        events.append("fetch_symbol")
        return {"symbol": symbol, "rows": [], "meta": {"status": "ok"}}

    def _validate_payload(**kwargs: Any) -> None:
        events.append("validate_payload")

    def _fail_finalizer(**kwargs: Any) -> None:
        events.append("finalize")
        raise OSError("artifact write failed")

    monkeypatch.setattr(prefetch, "_gateway_pool", _RecordingPool())
    monkeypatch.setattr(prefetch, "fetch_symbol", _fetch_symbol)
    monkeypatch.setattr(
        prefetch,
        "validate_required_data_payload_candidate",
        _validate_payload,
    )
    monkeypatch.setattr(
        prefetch,
        "finalize_required_data_quote_candidate",
        _fail_finalizer,
    )
    _patch_source_snapshot_writes(monkeypatch)

    result = prefetch._fetch_one_inprocess(
        {
            "symbol": "AAPL",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
        },
        base=tmp_path,
        shared_required=tmp_path / "required_data",
        opend_fetch_cfg=_opend_fetch_config(),
        batch_cfg=_batch_config(),
        expected_fetch_contract={},
    )

    assert result["ok"] is False
    assert events == [
        "get_gateway",
        "fetch_symbol",
        "validate_payload",
        "mark_success",
        "finalize",
    ]
