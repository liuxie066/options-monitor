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
    assert out["parsed"]["currency"] == "HKD"


def test_parse_futu_tencent_call_fill_infers_hkd_from_symbol(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "【成交提醒】成功卖出2张$腾讯 260629 510.00 购$，成交价格：9.48，此笔订单委托已全部成交，2026/04/29 13:15:24 (香港)。【富途证券(香港)】 lx"

    out = parse_option_message_text(msg, accounts=["lx", "sy"])

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "0700.HK"
    assert out["parsed"]["option_type"] == "call"
    assert out["parsed"]["side"] == "short"
    assert out["parsed"]["strike"] == 510.0
    assert out["parsed"]["exp"] == "2026-06-29"
    assert out["parsed"]["premium_per_share"] == 9.48
    assert out["parsed"]["currency"] == "HKD"


def test_parse_futu_us_fill_infers_usd_when_currency_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "【成交提醒】成功卖出1张$PLTR 260515 30.00 沽$，成交价格：1.25，此笔订单委托已全部成交，2026/04/26 15:30:00 (美国)。【富途证券】 lx"

    out = parse_option_message_text(msg, accounts=["lx", "sy"])

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "PLTR"
    assert out["parsed"]["currency"] == "USD"
