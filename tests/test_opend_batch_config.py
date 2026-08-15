from __future__ import annotations

from src.application.opend_fetch_config import (
    OpenDBatchConfig,
    opend_fetch_kwargs,
    resolve_opend_batch_config,
    resolve_opend_fetch_config,
)


def test_resolve_opend_batch_config_default() -> None:
    cfg = resolve_opend_batch_config(None)
    assert cfg.market_snapshot == 200


def test_resolve_opend_batch_config_custom_value() -> None:
    cfg = resolve_opend_batch_config({"runtime": {"opend_batch": {"market_snapshot": 50}}})
    assert cfg.market_snapshot == 50


def test_resolve_opend_batch_config_invalid_value_falls_back() -> None:
    cfg = resolve_opend_batch_config({"runtime": {"opend_batch": {"market_snapshot": "bad"}}})
    assert cfg.market_snapshot == 200


def test_resolve_opend_batch_config_clamps_to_minimum_one() -> None:
    assert resolve_opend_batch_config({"runtime": {"opend_batch": {"market_snapshot": 0}}}).market_snapshot == 1
    assert resolve_opend_batch_config({"runtime": {"opend_batch": {"market_snapshot": -5}}}).market_snapshot == 1


def test_resolve_opend_batch_config_reads_fallback_fields() -> None:
    cfg = resolve_opend_batch_config(
        {
            "runtime": {
                "opend_batch": {
                    "market_snapshot": 200,
                    "snapshot_fallback_max_codes": 50,
                    "snapshot_fallback_batch_size": 10,
                }
            }
        }
    )
    assert cfg.market_snapshot == 200
    assert cfg.market_snapshot_fallback_max_codes == 50
    assert cfg.market_snapshot_fallback_batch_size == 10


def test_resolve_opend_batch_config_defaults_when_absent() -> None:
    cfg = resolve_opend_batch_config({"runtime": {"opend_batch": {"market_snapshot": 200}}})
    normalized = OpenDBatchConfig.from_values(
        market_snapshot=200,
        market_snapshot_fallback_max_codes=0,
        market_snapshot_fallback_batch_size=0,
    )
    assert cfg.market_snapshot_fallback_max_codes == 100
    assert cfg.market_snapshot_fallback_batch_size == 20
    assert normalized.market_snapshot_fallback_max_codes == 0
    assert normalized.market_snapshot_fallback_batch_size == 20


def test_resolve_opend_fetch_config_prefers_unified_option_chain_limit() -> None:
    cfg = resolve_opend_fetch_config(
        {
            "runtime": {
                "option_chain_fetch": {"max_calls": 13, "window_sec": 12, "max_wait_sec": 11},
                "opend_rate_limits": {
                    "option_chain": {"max_calls": 3, "window_sec": 30, "max_wait_sec": 90},
                },
            }
        }
    )

    assert cfg["option_chain"] == {"max_calls": 3, "window_sec": 30.0, "max_wait_sec": 90.0}
def test_resolve_opend_fetch_config_keeps_legacy_option_chain_fetch() -> None:
    cfg = resolve_opend_fetch_config(
        {"runtime": {"option_chain_fetch": {"max_calls": 13, "window_sec": 12, "max_wait_sec": 11}}}
    )

    assert cfg["option_chain"] == {"max_calls": 13, "window_sec": 12.0, "max_wait_sec": 11.0}


def test_resolve_opend_fetch_config_uses_code_default_when_option_chain_absent() -> None:
    cfg = resolve_opend_fetch_config({"runtime": {"opend_rate_limits": {}}})

    assert cfg["option_chain"] == {"max_calls": 10, "window_sec": 30.0, "max_wait_sec": 90.0}
    assert cfg["market_snapshot"] == {"max_calls": 60, "window_sec": 30.0, "max_wait_sec": 30.0}
    assert cfg["option_expiration"] == {"max_calls": 60, "window_sec": 30.0, "max_wait_sec": 30.0}
    assert cfg["history_kline"] == {"max_calls": 60, "window_sec": 30.0, "max_wait_sec": 30.0}
    assert not any(key.startswith("history") for key in opend_fetch_kwargs(None))


def test_resolve_opend_fetch_config_reads_history_kline_limit() -> None:
    cfg = resolve_opend_fetch_config(
        {
            "runtime": {
                "opend_rate_limits": {
                    "history_kline": {
                        "max_calls": 12,
                        "window_sec": 30,
                        "max_wait_sec": 45,
                    }
                }
            }
        }
    )

    assert cfg["history_kline"] == {
        "max_calls": 12,
        "window_sec": 30.0,
        "max_wait_sec": 45.0,
    }
