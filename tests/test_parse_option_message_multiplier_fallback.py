from __future__ import annotations

from scripts.parse_option_message import parse_option_message_text


def test_parse_futu_tencent_fill_uses_resolved_multiplier_when_account_present(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93，此笔订单委托已全部成交，2026/04/09 13:10:25 (香港)。【富途证券(香港)】 lx"

    out = parse_option_message_text(msg, accounts=["lx", "sy"])

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "0700.HK"
    assert out["parsed"]["multiplier"] == 100
    assert out["parsed"]["account"] == "lx"
