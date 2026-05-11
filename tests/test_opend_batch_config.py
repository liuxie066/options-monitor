from __future__ import annotations

from src.application.opend_fetch_config import OpenDBatchConfig, resolve_opend_batch_config


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
