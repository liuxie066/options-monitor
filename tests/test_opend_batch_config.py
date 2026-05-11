from __future__ import annotations

from src.application.opend_fetch_config import resolve_opend_batch_config


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
