"""Cash queries keep broker cash and ledger collateral in the same scope."""
from copy import deepcopy

import pytest

from src.application import cash_headroom_query as query
from test_broker_candidate_contract import _portfolio
from test_broker_decision_history import config
from test_positions_context_builder_rates import _record


@pytest.mark.parametrize("broker", ["longbridge", "長橋證券"])
def test_default_cash_query_counts_longbridge_ledger_collateral(tmp_path, monkeypatch, broker):
    record = _record("NVDA", "short", "put", "USD", 8000)
    record["fields"]["broker"] = broker
    other = deepcopy(record)
    other["record_id"] = "futu-lot"
    other["fields"].update(broker="富途", cash_secured_amount=500)
    snapshot = {"snapshot_status": "trusted", "normalized_account": "lx",
                "account_position_lots": [record, other], "trade_events": []}
    monkeypatch.setattr(query, "load_account_portfolio_context", lambda **_: _portfolio())
    monkeypatch.setattr(query, "_load_option_position_records", lambda *a, **kw: ([], snapshot))
    result = query.query_sell_put_cash(account="lx", output_format="json", base_dir=tmp_path,
        runtime_config=config(), write_cache=False, no_exchange_rates=True)
    assert result["market"] == "长桥"
    assert result["cash_secured_used_usd"] == 8000
    assert result["cash_free_usd"] == 21000
    assert not (tmp_path / "output_shared").exists()


def test_interactive_pipeline_uses_configured_broker_for_ledger(tmp_path, monkeypatch):
    from src.application import pipeline_context as pipeline
    from src.application.positions.context_builder import build_context

    record = _record("NVDA", "short", "put", "USD", 8000)
    record["fields"]["broker"] = "长桥"
    other = deepcopy(record)
    other["record_id"] = "futu-lot"
    other["fields"].update(broker="富途", cash_secured_amount=500)
    snapshot = {"snapshot_status": "trusted", "normalized_account": "lx",
                "account_position_lots": [record, other], "trade_events": []}
    captured = {}

    def load_options(**kwargs):
        captured.update(kwargs)
        return build_context([], broker=kwargs["market"], account=kwargs["account"],
                             rates={}, decision_snapshot=snapshot), False

    monkeypatch.setattr(pipeline, "load_option_positions_context", load_options)
    monkeypatch.setattr(pipeline, "load_portfolio_context", lambda **_: _portfolio())
    monkeypatch.setattr(pipeline, "get_exchange_rates_or_fetch_latest", lambda **_: {})
    monkeypatch.setattr(pipeline, "load_exchange_rates", lambda **_: (None, None))
    _, options, _, _ = pipeline.build_pipeline_context(
        py="python", base=tmp_path, cfg=config(), report_dir=tmp_path / "reports",
        portfolio_timeout_sec=1, runtime={}, is_scheduled=False,
        state_dir=tmp_path / "state", log=lambda _: None, no_context=False, want_scan=True)
    assert captured["market"] == "长桥"
    assert options["cash_secured_total_by_ccy"]["USD"] == 8000
