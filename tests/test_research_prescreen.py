"""Existing scan fetches must survive filtering and failed quote qualification."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from src.application import opend_symbol_fetching as fetch
from src.application.opend_symbol_chain_fetching import SymbolOptionChainResult
from src.application.research.prescreen import publish_scan_prescreen, publish_watchlist_plan
from src.application.research import watchlist_scope as scope
from src.application.source_receipts import SourceReceiptError, validate_source_receipt


def run_fetch(tmp_path, monkeypatch, *, failure=False, empty=False, expiry="2026-06-19"):
    rows = (
        []
        if empty
        else [
            {
                "code": f"US.NVDA{date.fromisoformat(expiry):%y%m%d}P{strike * 1000}",
                "strike_time": expiry,
                "strike_price": strike,
                "option_type": "PUT",
                "stock_owner": "US.NVDA",
                "lot_size": 100,
                "provider_extra_chain_field": strike,
            }
            for strike in (90, 100, 110)
        ]
    )
    original = deepcopy(rows)
    bundle = SymbolOptionChainResult(
        rows=rows,
        frame=pd.DataFrame(rows),
        expirations_all=["2026-06-19", "2026-09-18"],
        expirations_pick=["2026-06-19"],
        fetch_meta={
            "status": "ok",
            "source_outcome": "success_empty" if empty else "success_rows",
            "reason_code": "no_contract_rows" if empty else None,
            "expiration_statuses": {"2026-06-19": "empty" if empty else "fetched"},
            "source_observed_at": "2026-05-20T13:00:00Z",
            "completed_at_utc": "2026-05-20T13:00:01Z",
        },
    )
    monkeypatch.setattr(fetch, "get_trading_date", lambda _: date(2026, 5, 20))
    monkeypatch.setattr(fetch, "fetch_symbol_option_chain", lambda **_: bundle)
    monkeypatch.setattr(fetch, "rate_limited_opend_call", lambda **kw: kw["call"]())
    monkeypatch.setattr("src.application.opend_utils.load_runtime_symbol_aliases", lambda *_: {})
    calls = []

    class Gateway:
        def get_snapshot(self, codes):
            calls.append(list(codes))
            if failure:
                raise TimeoutError("synthetic snapshot failure")
            return pd.DataFrame(
                [
                    {
                        "code": code,
                        "bid_price": 1.0,
                        "ask_price": 1.1,
                        "last_price": 1.05,
                        "provider_extra_quote_field": "preserve me",
                    }
                    for code in codes
                ]
            )

    payload = fetch.fetch_symbol_request(
        fetch.FetchSymbolRequest(
            symbol="NVDA",
            base_dir=tmp_path,
            gateway=Gateway(),
            spot_override=100,
            option_types="put",
            min_strike=95,
            max_strike=105,
            no_retry=True,
            snapshot_fallback_max_codes=0,
        )
    )
    assert rows == original
    return payload, calls


def test_prescreen_keeps_pre_strike_chain_and_full_provider_columns(tmp_path, monkeypatch):
    payload, calls = run_fetch(tmp_path, monkeypatch)
    capture = payload["meta"]["research_prescreen"]
    assert calls == [["US.NVDA260619P100000"]]
    assert [row["strike_price"] for row in capture["chain"]["rows"]] == [90, 100, 110]
    assert capture["chain"]["available_expirations"] == ["2026-06-19", "2026-09-18"]
    assert capture["chain"]["requested_expirations"] == ["2026-06-19"]
    assert capture["chain"]["meta"]["source_observed_at"] == "2026-05-20T13:00:00Z"
    quotes = capture["snapshots"]
    assert quotes["requested_codes"] == ["US.NVDA260619P100000"]
    assert quotes["provider_responses"][0]["rows"][0]["provider_extra_quote_field"] == "preserve me"
    assert "provider_extra_quote_field" not in quotes["rows"][0]
    assert capture["missing_reasons"] == []
    assert capture["started_at_utc"] and capture["completed_at_utc"]


@pytest.mark.parametrize("failure,empty", [(False, False), (True, False), (False, True)])
def test_capture_receipt_is_private_immutable_and_not_quote_authority(tmp_path, monkeypatch, failure, empty):
    payload, calls = run_fetch(tmp_path, monkeypatch, failure=failure, empty=empty)
    capture = payload["meta"]["research_prescreen"]
    if failure:
        assert capture["snapshots"]["missing_codes"] == ["US.NVDA260619P100000"]
        assert capture["snapshots"]["batch_receipts"][0]["status"] == "failed"
        assert payload["meta"]["status"] == "error"
    if empty:
        assert calls == []
        assert capture["chain"]["rows"] == []
        assert capture["snapshots"] is None
        assert capture["missing_reasons"] == ["snapshots_not_attempted"]
    root = tmp_path / "required"
    params = dict(producer_root=root, producer_run_id="synthetic-run", payload=payload, fetch_plan={"symbol": "NVDA"})
    receipt = publish_scan_prescreen(**params)
    assert publish_scan_prescreen(**params) == receipt
    assert receipt["source_kind"] == "research_supplemental"
    assert receipt["account"] is None
    assert receipt["source_observed_at"] == capture["completed_at_utc"]
    raw = root / receipt["payload_relpath"]
    assert raw.stat().st_mode & 0o777 == 0o600
    stored = json.loads(raw.read_bytes())
    assert stored["capture"] == capture
    assert stored["fetch_plan"] == {"symbol": "NVDA"}
    assert not (root / "source_receipts" / "quotes").exists()
    with pytest.raises(SourceReceiptError):
        validate_source_receipt(receipt, producer_root=root, now=receipt["completed_at"], expected_source_kind="quotes")
    raw.write_bytes(b"{}")
    with pytest.raises(SourceReceiptError):
        publish_scan_prescreen(**params)


def test_planned_roster_keeps_unfetched_symbols_without_exporting_config(tmp_path):
    plan = {"planning_status": "not_attempted", "symbols": [{"symbol": "NVDA"}, {"symbol": "0700.HK"}]}
    receipt = publish_watchlist_plan(producer_root=tmp_path, producer_run_id="synthetic-run", plan=plan)
    stored = json.loads((tmp_path / receipt["payload_relpath"]).read_bytes())
    assert stored["plan"] == plan
    assert receipt["included_markets"] == ["HK", "US"]
    assert receipt["account"] is None
    assert receipt["source_kind"] == "research_supplemental"
    assert len(list(tmp_path.rglob("receipt.json"))) == 1


def native_input(root, monkeypatch, *, captures=1, run_id="synthetic-run"):
    from tests.test_watchlist_research_scope import bundle

    frozen = publish_watchlist_plan(
        producer_root=root,
        producer_run_id="synthetic-run",
        plan={"symbols": [{"symbol": "NVDA"}, {"symbol": "0700.HK"}]},
    )

    def ref(receipt):
        path = root / Path(receipt["payload_relpath"]).parent / "receipt.json"
        raw = path.read_bytes()
        return {
            "path": str(path.relative_to(root)),
            "sha256": scope.quote._sha256(raw),
            "pointer": "",
            "observed_at_utc": receipt["completed_at"],
        }

    capture_refs = []
    expiry = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    for _ in range(captures):
        payload, _ = run_fetch(root, monkeypatch, expiry=expiry)
        receipt = publish_scan_prescreen(
            producer_root=root,
            producer_run_id=run_id,
            payload=payload,
            fetch_plan=bundle(("put", 80, 100), ("call", 100, 120), symbol="NVDA"),
        )
        capture_refs.append(ref(receipt))
    data = {
        "schema_version": scope.SCAN_INPUT_SCHEMA,
        "scope_id": "scan-scope",
        "as_of_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fx_pairs": ["HKDCNY", "USDCNY"],
        "watchlist": ref(frozen),
        "captures": capture_refs,
        "previous_tracking": None,
    }
    return data


def build_native(root, data, out=None):
    raw = scope.series._bytes(data)
    path = root / "native-input.json"
    path.write_bytes(raw)
    return scope.build_watchlist_scope(input_file=path, expected_input_sha256=scope.quote._sha256(raw), output_dir=out)


def test_native_scan_receipts_build_scope_without_rewriting_originals(tmp_path, monkeypatch):
    data = native_input(tmp_path, monkeypatch, captures=2)
    originals = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*.json")}
    out = tmp_path / "out"
    result = build_native(tmp_path, data, out)
    assert result["counts"]["US"]["contracts"] == 2
    assert result["counts"]["HK"]["contracts"] == 0
    assert result["status"] == "incomplete"
    assert "HK.00700:chain_not_captured" in result["missing_reasons"]
    assert "HK.00700:owner_window_missing" in result["missing_reasons"]
    assert "US.NVDA:chain_coverage_unverified" in result["missing_reasons"]
    for path, raw in originals.items():
        assert (tmp_path / path).read_bytes() == raw
        assert (out / path).read_bytes() == raw
    assert json.loads((out / "derived-input.json").read_bytes())["symbols"][0]["underlier_code"] == "US.NVDA"


def test_native_scan_missing_all_captures_keeps_roster_gaps(tmp_path, monkeypatch):
    result = build_native(tmp_path, native_input(tmp_path, monkeypatch, captures=0))
    assert result["counts"]["US"]["contracts"] == 0
    assert result["counts"]["HK"]["contracts"] == 0
    assert result["missing_reasons"] == [
        "HK.00700:chain_not_captured",
        "HK.00700:owner_window_missing",
        "US.NVDA:chain_not_captured",
        "US.NVDA:owner_window_missing",
    ]


def test_native_roster_deduplicates_endpoints_and_retains_each_owner_window(tmp_path, monkeypatch):
    from tests.test_watchlist_research_scope import bundle

    data = native_input(tmp_path, monkeypatch)
    receipt_path = tmp_path / data["watchlist"]["path"]
    receipt = json.loads(receipt_path.read_bytes())
    # A real owner summary can contain the same symbol at multiple endpoints.
    frozen = publish_watchlist_plan(
        producer_root=tmp_path,
        producer_run_id=receipt["producer_run_id"],
        plan={
            "symbols": [
                {"symbol": "NVDA", "fetch_plan": bundle(("put", 80, 100), symbol="NVDA")},
                {"symbol": "NVDA", "fetch_plan": bundle(("call", 100, 120), symbol="NVDA")},
            ]
        },
    )
    path = Path(frozen["payload_relpath"]).parent / "receipt.json"
    data["watchlist"] = {
        "path": str(path),
        "sha256": scope.quote._sha256((tmp_path / path).read_bytes()),
        "pointer": "",
        "observed_at_utc": frozen["completed_at"],
    }
    data["as_of_utc"] = datetime.now(timezone.utc).isoformat()
    data["captures"] = []
    result = build_native(tmp_path, data, tmp_path / "deduplicated")
    derived = json.loads((tmp_path / "deduplicated/derived-input.json").read_bytes())
    assert len(derived["symbols"]) == 1
    assert len(derived["symbols"][0]["owner_windows"]) == 2
    assert [(w["min_strike"], w["max_strike"]) for w in result["windows"]] == [(95, 126), (76, 105)]
    assert result["missing_reasons"] == ["US.NVDA:chain_not_captured"]


@pytest.mark.parametrize("change", ["tamper", "run", "availability", "pointer", "duplicate"])
def test_native_receipts_reject_wrong_sources(tmp_path, monkeypatch, change):
    data = native_input(tmp_path, monkeypatch, run_id="other-run" if change == "run" else "synthetic-run")
    if change == "tamper":
        path = tmp_path / data["captures"][0]["path"]
        path.write_bytes(b"{}")
    elif change == "availability":
        data["captures"][0]["observed_at_utc"] = "2026-01-01T00:00:00Z"
    elif change == "pointer":
        data["captures"][0]["pointer"] = "/source_kind"
    elif change == "duplicate":
        data["captures"].append(data["captures"][0])
    with pytest.raises((ValueError, SourceReceiptError)):
        build_native(tmp_path, data)
