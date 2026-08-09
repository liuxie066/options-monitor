"""Managed collector boundary and public-CLI absence tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.ai_decision_advice import managed_collector
from src.application.ai_decision_advice.collector import CollectorRunSummary
from src.application.ai_decision_advice.evidence_store import append_evidence_records
from src.application.ai_decision_advice.identity import (
    build_observation_set,
    publish_observation_partition,
)
from src.interfaces.cli import ai_evidence_collector
from src.interfaces.cli import main as public_cli


def _config(*, enabled: bool) -> dict:
    body = {
        "accounts": {"lx": {}},
        "symbols": [{"symbol": "NVDA"}],
    }
    if enabled:
        body["ai_decision_advice"] = {"enabled": True}
    return body


def _patch_configs(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(
        managed_collector,
        "load_runtime_config",
        lambda config_key, expected_market=None: (
            Path(f"config.{config_key}.json"),
            _config(enabled=enabled),
        ),
    )


def test_managed_collector_skips_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_configs(monkeypatch, enabled=False)
    result = managed_collector.run_managed_collector(
        config_keys=["us", "hk"],
        runtime_root=tmp_path / "runtime",
    )
    assert result == {"status": "skipped", "reason": "ai_decision_advice_disabled"}


def test_provider_adapter_reduces_response_to_attributed_audit_and_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "status": "completed",
        "output": [
            {
                "type": "web_search_call",
                "id": "must-not-persist",
                "action": {
                    "type": "search",
                    "query": "NVDA NVIDIA latest filing",
                    "sources": [
                        {"type": "url", "url": "https://example.com/fact"}
                    ],
                },
                "status": "completed",
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"results":[]}',
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/fact",
                                "title": "Fact",
                            }
                        ],
                    }
                ],
            },
        ],
        "provider_private": "must-not-persist",
    }
    monkeypatch.setattr(
        managed_collector,
        "create_deepseek_response",
        lambda **kwargs: response,
    )
    result = managed_collector.build_deepseek_evidence_runner("not-a-real-key")(
        "instructions",
        {
            "symbols": [
                {
                    "symbol": "NVDA",
                    "company_name": "NVIDIA",
                    "aliases": [],
                }
            ]
        },
        None,
        1,
    )
    assert result.output_text == '{"results":[]}'
    assert result.web_search_audit == {
        "count": 1,
        "unattributed_count": 0,
        "auxiliary_count": 0,
        "status_counts": {
            "completed": 1,
            "failed": 0,
            "in_progress": 0,
            "unknown": 0,
        },
        "symbols": {
            "NVDA": {
                "completed": 1,
                "failed": 0,
                "in_progress": 0,
                "unknown": 0,
            }
        },
    }
    assert result.native_citations == (
        {"url": "https://example.com/fact", "title": "Fact"},
    )
    assert result.native_search_sources == (
        {"symbol": "NVDA", "url": "https://example.com/fact"},
    )
    assert "must-not-persist" not in repr(result)


def test_managed_collector_fails_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_configs(monkeypatch, enabled=True)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = managed_collector.run_managed_collector(
        config_keys=["us"],
        runtime_root=tmp_path / "runtime",
    )
    assert result == {"status": "failed", "reason": "missing_api_key"}


def test_all_identity_unavailable_is_a_failed_managed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_configs(monkeypatch, enabled=True)
    monkeypatch.setattr(managed_collector, "resolve_api_key", lambda: "test-key")

    def must_not_run(*args, **kwargs):
        raise AssertionError("model must not run without symbol identity")

    result = managed_collector.run_managed_collector(
        config_keys=["us"],
        runtime_root=tmp_path / "runtime",
        market_snapshot_provider=lambda market, symbols: {},
        model_runner_factory=lambda key: must_not_run,
    )
    assert result["status"] == "failed"
    assert result["reason"] == "no_evidence_refresh_completed"
    assert result["summary"]["completed_count"] == 0
    assert result["summary"]["identity_unavailable_count"] == 1


def test_managed_collector_uses_opend_basic_info_when_snapshot_name_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_configs(monkeypatch, enabled=True)
    monkeypatch.setattr(managed_collector, "resolve_api_key", lambda: "test-key")
    captured: dict = {}

    def collect(**kwargs):
        captured["identity_snapshot"] = kwargs["identity_snapshot"]
        return CollectorRunSummary(
            evidence_run_id=kwargs["evidence_run_id"],
            started_at="2026-08-09T04:00:00+00:00",
            completed_symbols=list(kwargs["queue_symbols"]),
        )

    monkeypatch.setattr(managed_collector, "run_evidence_collector", collect)
    result = managed_collector.run_managed_collector(
        config_keys=["us"],
        runtime_root=tmp_path / "runtime",
        now=managed_collector.datetime(
            2026,
            8,
            9,
            4,
            tzinfo=managed_collector.timezone.utc,
        ),
        market_snapshot_provider=lambda market, symbols: {
            "US.NVDA": {"code": "US.NVDA", "name": ""}
        },
        basic_info_provider=lambda symbols: [
            {
                "code": "US.NVDA",
                "name": "NVIDIA Corporation",
                "exchange_type": "NASDAQ",
            }
        ],
        model_runner_factory=lambda key: must_not_call,
    )

    identity = captured["identity_snapshot"]["symbols"][0]
    assert identity["status"] == "resolved"
    assert identity["name"] == "NVIDIA Corporation"
    assert result["status"] == "completed"


def test_managed_queue_prioritizes_symbols_without_prior_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(enabled=True)
    config["symbols"] = [{"symbol": "AAPL"}, {"symbol": "NVDA"}]
    monkeypatch.setattr(
        managed_collector,
        "load_runtime_config",
        lambda config_key, expected_market=None: (Path("config.us.json"), config),
    )
    monkeypatch.setattr(managed_collector, "resolve_api_key", lambda: "test-key")
    runtime = tmp_path / "runtime"
    append_evidence_records(
        base=runtime,
        records=[
            {
                "kind": "symbol_status",
                "symbol": "AAPL",
                "last_checked_at": "2026-08-09T03:00:00+00:00",
                "search_status": "failed",
            }
        ],
        evidence_run_id="previous",
        appended_at="2026-08-09T03:00:00+00:00",
    )
    captured: dict = {}

    def collect(**kwargs):
        captured["queue_symbols"] = list(kwargs["queue_symbols"])
        return CollectorRunSummary(
            evidence_run_id=kwargs["evidence_run_id"],
            started_at="2026-08-09T04:00:00+00:00",
            completed_symbols=list(kwargs["queue_symbols"]),
        )

    monkeypatch.setattr(managed_collector, "run_evidence_collector", collect)
    result = managed_collector.run_managed_collector(
        config_keys=["us"],
        runtime_root=runtime,
        now=managed_collector.datetime(2026, 8, 9, 4, tzinfo=managed_collector.timezone.utc),
        market_snapshot_provider=lambda market, symbols: {
            symbol: {"name": f"{symbol} Inc", "exchange_type": "NASDAQ"}
            for symbol in symbols
        },
        model_runner_factory=lambda key: must_not_call,
    )
    assert captured["queue_symbols"] == ["NVDA", "AAPL"]
    assert result["status"] == "completed"


def must_not_call(*args, **kwargs):
    raise AssertionError("unexpected model call")


def test_opend_identity_provider_batches_and_keeps_successful_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {
                    "source": "futu",
                    "host": "127.0.0.1",
                    "port": 11111,
                },
            }
        ]
    }
    calls: list[list[str]] = []

    class Gateway:
        closed = False

        def get_snapshot(self, codes):
            calls.append(list(codes))
            if len(calls) == 2:
                raise TimeoutError("second batch failed")
            return [{"code": code, "name": code} for code in codes]

        def close(self):
            self.closed = True

    gateway = Gateway()
    monkeypatch.setattr(
        managed_collector,
        "futu_underlier_code",
        lambda symbol: f"US.{symbol}",
    )
    provider = managed_collector._opend_market_snapshot_provider(
        {"US": config},
        gateway_factory=lambda **kwargs: gateway,
    )
    symbols = [f"S{index:03d}" for index in range(201)]
    rows = provider("US", symbols)
    assert [len(batch) for batch in calls] == [200, 1]
    assert len(rows) == 200
    assert gateway.closed is True


def test_opend_basic_info_provider_uses_explicit_code_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "symbols": [
            {
                "symbol": "NVDA",
                "fetch": {
                    "source": "futu",
                    "host": "127.0.0.1",
                    "port": 11111,
                },
            }
        ]
    }
    calls: list[tuple[str, list[str]]] = []

    class Gateway:
        closed = False

        def get_stock_basicinfo(self, *, market, codes):
            calls.append((market, list(codes)))
            return [
                {
                    "code": code,
                    "name": "NVIDIA Corporation",
                    "exchange_type": "NASDAQ",
                }
                for code in codes
            ]

        def close(self):
            self.closed = True

    gateway = Gateway()
    monkeypatch.setattr(
        managed_collector,
        "futu_underlier_code",
        lambda symbol: f"US.{symbol}",
    )
    provider = managed_collector._opend_basic_info_provider(
        {"US": config},
        gateway_factory=lambda **kwargs: gateway,
    )

    rows = provider(["NVDA"])

    assert calls == [("US", ["US.NVDA"])]
    assert rows[0]["code"] == "US.NVDA"
    assert gateway.closed is True


def test_dry_run_uses_config_only_when_observation_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_configs(monkeypatch, enabled=True)
    result = managed_collector.run_managed_collector(
        config_keys=["us"],
        runtime_root=tmp_path / "runtime",
        dry_run=True,
    )
    assert result == {
        "status": "dry_run",
        "observation_source": "config_fallback",
        "observation_count": 1,
    }


def test_dry_run_prefers_anonymous_observation_and_never_exposes_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_configs(monkeypatch, enabled=True)
    runtime = tmp_path / "runtime"
    publish_observation_partition(
        base=runtime,
        market="HK",
        observed=build_observation_set(scan_symbols=["0700.HK"]),
        generation="hk-1",
    )
    result = managed_collector.run_managed_collector(
        config_keys=["us"],
        runtime_root=runtime,
        dry_run=True,
    )
    assert result["observation_source"] == "anonymous_snapshot"
    assert result["observation_count"] == 1
    assert "NVDA" not in json.dumps(result)
    assert "priority" not in json.dumps(result)


def test_internal_wrapper_delegates_without_business_logic(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    seen: dict = {}

    def run(**kwargs):
        seen.update(kwargs)
        return {"status": "completed"}

    monkeypatch.setattr(ai_evidence_collector, "run_managed_collector", run)
    exit_code = ai_evidence_collector.main(["--config-key", "us"])
    assert exit_code == 0
    assert seen["config_keys"] == ["us"]
    assert json.loads(capsys.readouterr().out) == {"status": "completed"}


def test_internal_wrapper_returns_nonzero_without_leaking_error(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def boom(**kwargs):
        raise FileNotFoundError("private config path")

    monkeypatch.setattr(ai_evidence_collector, "run_managed_collector", boom)
    exit_code = ai_evidence_collector.main(["--config-key", "us"])
    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "status": "failed",
        "reason": "collector_error",
        "error_type": "FileNotFoundError",
    }
    assert "private config path" not in json.dumps(out)


def test_public_om_cli_does_not_dispatch_collector(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        public_cli.main(["ai-evidence-collector"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        public_cli.parse_args(["--help"])
    assert "ai-evidence-collector" not in capsys.readouterr().out
