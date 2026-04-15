from __future__ import annotations

from pathlib import Path

from scripts.parse_option_message import _infer_multiplier_if_missing, parse_option_message_text


def test_infer_multiplier_uses_builtin_for_tencent_when_cache_and_opend_missing() -> None:
    multiplier = _infer_multiplier_if_missing(
        symbol="0700.HK",
        multiplier=None,
        repo_base=Path("/tmp/nonexistent-options-monitor"),
    )

    assert multiplier == 100


def test_parse_futu_tencent_fill_uses_builtin_multiplier_when_account_present() -> None:
    msg = "【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93，此笔订单委托已全部成交，2026/04/09 13:10:25 (香港)。【富途证券(香港)】 lx"

    out = parse_option_message_text(msg)

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "0700.HK"
    assert out["parsed"]["multiplier"] == 100
    assert out["parsed"]["account"] == "lx"
