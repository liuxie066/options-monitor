import pytest

from src.application.required_data_observability import extract_fetch_payload_metrics


def test_extract_fetch_payload_metrics_sums_provider_work_without_double_counting_snapshots() -> None:
    payload = {
        "expiration_count": 2,
        "rows": [{"contract_symbol": code} for code in "ABCDEFGH"],
        "meta": {
            "snapshot_requested_codes": 8,
            "snapshot_returned_codes": 8,
            "requests": [
                {
                    "expiration_opend_calls": 1,
                    "expiration_cache_hits": 2,
                    "opend_call_count": 3,
                    "rate_gate_wait_sec": 1.25,
                    "from_cache_expirations": ["2026-09-18"],
                    "stale_cache_expirations": ["2026-09-25"],
                    "fetched_expirations": ["2026-10-16"],
                    "snapshot_requested_codes": 4,
                    "snapshot_opend_call_count": 1,
                    "spot_snapshot_opend_calls": 1,
                    "spot_snapshot_requested_codes": 1,
                    "snapshots_rows": 4,
                    "snapshot_fallback_filled": 1,
                },
                {
                    "expiration_opend_calls": 2,
                    "expiration_cache_hits": 3,
                    "opend_call_count": 4,
                    "rate_gate_wait_sec": 0.75,
                    "from_cache_expirations": ["2026-09-18", "2026-09-25"],
                    "fetched_expirations": ["2026-10-16", "2026-11-20"],
                    "snapshot_requested_codes": 6,
                    "snapshot_opend_call_count": 2,
                    "snapshots_rows": 6,
                    "snapshot_fallback_failed": 2,
                },
            ]
        },
    }

    assert extract_fetch_payload_metrics(payload) == {
        "rows": 8,
        "expiration_count": 2,
        "expiration_opend_calls": 3,
        "expiration_cache_hits": 5,
        "option_chain_opend_calls": 7,
        "option_chain_rate_gate_wait_sec": 2.0,
        "option_chain_cache_hits": 3,
        "option_chain_stale_cache_hits": 1,
        "option_chain_fetched_expirations": 3,
        "snapshot_requested_codes": 8,
        "snapshot_opend_calls": 3,
        "spot_snapshot_opend_calls": 1,
        "market_snapshot_opend_calls": 4,
        "spot_snapshot_requested_codes": 1,
        "snapshot_rows": 8,
        "snapshot_fallback_filled": 1,
        "snapshot_fallback_failed": 2,
    }


def test_extract_fetch_payload_metrics_rejects_malformed_child_metadata() -> None:
    payload = {
        "meta": {
            "requests": [
                {"opend_call_count": 1},
                "malformed child",
            ]
        }
    }

    with pytest.raises(
        ValueError,
        match="required-data child metrics metadata is invalid",
    ):
        extract_fetch_payload_metrics(payload)
