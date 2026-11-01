"""Active facade through real planning, prefetch, sealing and scheduled evaluator.

Only provider transport and the subprocess boundary are replaced. Enrichment
runs its real implementation in process so the isolated provider is visible.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application import position_advice as service
from test_market_data_provider import RecordingProvider, install, no_live_io  # noqa: F401
from test_position_advice_service import config
from test_required_data_prefetch_inprocess import _strict_success_rows_for_fetch


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    from src.application import close_advice_required_data as plans, opend_utils
    from src.application.multi_tick import required_data_prefetch as prefetch
    from src.application.opening_quote_evidence import normalize_underlier_observation

    now = datetime.now(timezone.utc)
    expiry = (now + timedelta(days=21)).date().isoformat()
    observation = normalize_underlier_observation(code="US.NVDA", market="US", snapshot_row={
        "code": "US.NVDA", "last_price": 110, "update_time": now.isoformat(),
        "sec_status": "NORMAL", "suspension": False,
    }, market_state_row={"code": "US.NVDA", "market_state": "MORNING"}, now_utc=now)

    class Provider(RecordingProvider):
        fail_fetch = False
        right = "LV1"

        def fetch_symbol_request(self, request, **kwargs):
            self.calls.append(("fetch", request))
            if self.fail_fetch:
                raise TimeoutError("isolated provider failure")
            payload = _strict_success_rows_for_fetch(request.symbol, vars(request), meta={"source": self.name})
            observed = datetime.now(timezone.utc).isoformat()
            for row in payload["rows"]:
                row.update(bid=.1, ask=.15, delta=-.2, currency="USD", market="US", multiplier=100,
                    snapshot_requested_at_utc=observed, snapshot_received_at_utc=observed,
                    spot_observed_at_utc=now.isoformat(), underlier_observation_status="ready",
                    underlier_sec_status="NORMAL", underlier_suspension=False, market_state="MORNING",
                    option_sec_status="NORMAL", option_suspension=False)
            return payload

        def get_market_state(self, gateway, codes):
            return [{"code": code, "market_state": "MORNING"} for code in codes]

        def get_quote_rights(self, gateway, *, market):
            return {"status": "ok", "market": market, "underlier_right": "LV1", "option_right": self.right}

        def get_trading_days_with_receipt(self, gateway, *, market, start, end):
            start_date = datetime.fromisoformat(start).date()
            end_date = datetime.fromisoformat(end).date()
            return {"retcode": 0, "coverage_complete": True, "pagination_complete": True, "page_count": 1,
                "rows": [{"time": (start_date + timedelta(days=i)).isoformat(), "trade_date_type": "WHOLE"}
                         for i in range((end_date - start_date).days + 1)]}

    provider = install(monkeypatch, Provider({"meta": {"underlier_observation": observation.to_dict()}, "expirations": [expiry]}))
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: now.date())
    monkeypatch.setattr(prefetch, "_gateway_pool", SimpleNamespace(get_gateway=lambda **kw: provider.gateway,
        mark_success=lambda: None, mark_failure=lambda *a, **kw: None,
        close_current_thread=provider.gateway.close, close_registered=lambda: None))
    def enrich(**kwargs):
        plans.enrich_close_advice_required_data_plan(plan_path=kwargs["plan_path"], expected_run_id=kwargs["expected_run_id"])
    monkeypatch.setattr(plans, "enrich_close_advice_required_data_plan_bounded", enrich)
    cfg = config()
    cfg["market_data"] = {"provider": provider.name}
    cfg["runtime"] = {"prefetch_max_workers": 1}
    request = {"account": "sample", "positions": [{"input_id": "manual", "input_as_of_utc": now.isoformat(),
        "symbol": "NVDA", "option_type": "put", "expiration": expiry, "strike": 100,
        "contracts_open": 1, "premium": 2, "currency": "USD", "multiplier": 100}]}
    def evaluate():
        return service.evaluate_position_advice(request, config=cfg, config_path=tmp_path / "config.us.json",
            base_dir=Path(__file__).resolve().parents[1], runtime_root=tmp_path)
    return evaluate, provider, tmp_path


def test_active_entry_seals_selected_holdings_and_reuses_scheduled_evaluator(pipeline):
    evaluate, provider, root = pipeline
    result = evaluate()
    assert result["returned_count"] == 1, result
    row = result["rows"][0]
    assert row["assessment_mode"] == "current", result
    assert row["recommendation_state"] == "close", result
    assert row["current_actionable"] is True
    assert isinstance(row["net_capture_ratio"], float)
    assert isinstance(row["is_otm"], bool)
    assert row["required_data_payload_sha256"] and row["required_data_receipt_hash"]
    assert row["input_source"] == "user_provided" and not row["position_lot_id"]
    assert row["policy_version"] == "remaining_yield_capture.v3"
    assert result["notify_rows"] == 0
    assert result["evidence"]["report_validation"]["ok"]
    assert [value for name, value in provider.calls if name == "fetch"]
    assert not (root / "output_runs").exists()
    assert not list(root.rglob("*.sqlite*"))


@pytest.mark.parametrize("right", ["BMP", "NO", "UNKNOWN"])
def test_active_real_pipeline_rejects_unproven_realtime_option_rights(pipeline, right):
    evaluate, provider, _ = pipeline
    provider.right = right
    result = evaluate()
    assert result["rows"][0]["recommendation_state"] == "not_evaluable", result
    assert result["rows"][0]["assessment_mode"] == "unavailable", result
    assert result["notify_rows"] == 0


def test_active_real_pipeline_provider_failure_is_unavailable(pipeline):
    evaluate, provider, _ = pipeline
    provider.fail_fetch = True
    result = evaluate()
    assert result["rows"][0]["recommendation_state"] == "not_evaluable", result
    assert result["status"] == "partial"
    assert result["notify_rows"] == 0
