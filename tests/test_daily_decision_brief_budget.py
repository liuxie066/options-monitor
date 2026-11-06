from __future__ import annotations

from copy import deepcopy

import pytest

from src.application.channels.feishu_notification_renderer import (
    FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES,
    feishu_notification_blocks_fit,
    render_feishu_notification_card,
)
from src.application.daily_decision_brief_renderer import (
    _Block,
    _display_evidence,
    _fit_blocks,
    _render_user_view_blocks,
    build_daily_brief_user_view,
    select_daily_brief_notification,
)
from src.infrastructure.feishu_bot import (
    FEISHU_POST_REQUEST_BUDGET_BYTES,
    FEISHU_SEND_REQUEST_BUDGET_BYTES,
    build_message_payload,
    build_post_message_payload,
    feishu_request_body_bytes,
)
from tests.test_daily_decision_brief_renderer import (
    _brief,
    _candidate,
    _render_event_risk,
    _scheduled_context,
)


def test_fit_blocks_removes_whole_units_in_tier_and_rank_order() -> None:
    required = [_Block("账户｜示例\n状态｜固定报告"), _Block("缺口｜报价不完整", "gap"),
                _Block("展示范围｜另有 2 条未展开", "omission")]
    action = _Block("持仓动作｜旧约→新约\n净收入｜$75\n有效期｜20:00", "action", 1, True)
    preferred = _Block("首选开仓｜长合约标签" + "中" * 400 + "\n净收入｜$480", "candidate", 5, True)
    lower = _Block("次选开仓｜长合约标签" + "文" * 400 + "\n净收入｜$390", "candidate", 1, True)
    info = _Block("事件｜财报将至" + "知" * 200, "info", 0, True)
    aux = _Block("资金｜$1,000" + "辅" * 200, "aux", 0, True)
    rows = [*required, preferred, lower, info, action, aux]
    kept, removed, hard = _fit_blocks(rows, 700)
    assert not hard
    assert len("\n\n".join(kept)) <= 700
    assert removed == [aux, info, lower]
    assert all(any(item is row for row in kept) for item in [*required, preferred, action])
    assert "撤去 1 条辅助信息、1 条知情卡、1 条开仓卡" in kept[-1]
    for row in rows:
        assert str(row) in "\n\n".join(kept) or row in removed


def test_required_blocks_survive_even_if_no_message_can_fit() -> None:
    required = [_Block("状态｜" + "中" * 12_000), _Block("缺口｜数据不足", "gap"),
                _Block("展示范围｜另有 2 条", "omission")]
    rows = [*required, _Block("动作｜完整卡\n净现金｜$50", "action", 0, True)]
    kept, removed, hard = _fit_blocks(rows, 12_000)
    assert hard and removed == [rows[-1]]
    assert all(any(item is row for row in kept) for item in required)


def _five_candidate_brief(*, long: bool = False) -> tuple[dict, list[str]]:
    brief = _brief()
    brief["positions"] = []
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}
    brief["candidate_index"] = []
    identities = []
    for index, symbol in enumerate(("MSFT", "NVDA", "AAPL", "PDD", "SPCX"), start=1):
        symbol = symbol + ("中文长合约" * 450 if long else "")
        row = _candidate(rank=index, symbol=symbol, option_type="put", expiration="2026-08-21",
                         strike=100 + index, capacity=1)
        row["event_risk"] = _render_event_risk("confirmed_none")
        identity = f"candidate:v1:lx:US:US.{symbol}:sell_put"
        identities.append(identity)
        brief["candidates"]["sell_put"].append(row)
        brief["candidate_index"].append({"identity": identity, "symbol": symbol,
                                         "strategy_family": "sell_put", "representative": row})
    return brief, identities


def test_display_evidence_follows_surviving_summary_and_full_cards() -> None:
    brief, identities = _five_candidate_brief()
    selection = select_daily_brief_notification(
        brief, delivery_kind="candidate_alert", candidate_identities=identities,
        context=_scheduled_context(),
    )
    assert [row["state"] for row in selection["display_evidence"]] == [
        "expanded", "expanded", "expanded", "summary_notified", "summary_notified",
    ]
    blocks = selection["markdown_blocks"]
    assert "另有 2 个新增候选未展开" in selection["text"]
    no_summary = [row for row in blocks if not row.summary_family]
    evidence = _display_evidence(brief, identities, blocks, no_summary)
    assert [row["state"] for row in evidence] == [
        "expanded", "expanded", "expanded", "not_shown", "not_shown",
    ]
    assert _display_evidence({}, ["position-roll:v1:lx:US:roll:id"], [],
                             [_Block("另有 1 条", "omission", summary_family="all")])[0]["state"] == "not_shown"


def test_common_selection_fits_unicode_card_post_and_frozen_text_atomically() -> None:
    brief, identities = _five_candidate_brief(long=True)
    original = deepcopy(brief)
    context = _scheduled_context()
    selection = select_daily_brief_notification(
        brief, delivery_kind="fixed_report", candidate_identities=identities,
        context=context, limits={"max_candidates_per_strategy": 5},
        fits=lambda rows, text: feishu_notification_blocks_fit(rows, text, open_id="ou_example"),
    )
    blocks, text = selection["markdown_blocks"], selection["text"]
    markdown = "\n\n".join(blocks)
    assert len(text) <= 12_000 and len(markdown) <= 12_000
    assert len(markdown.encode("utf-8")) <= FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES
    envelope = render_feishu_notification_card(
        markdown=markdown, markdown_blocks=blocks, fallback_text=text,
        fallback_open_id="ou_example", preserve_blocks=True,
    )
    assert envelope["fallback"]["markdown"] == envelope["text"] == text
    assert not envelope["render_meta"]["truncated"]
    primary = envelope["transport"]["content"]
    assert "\n\n".join(row["content"] for row in primary["body"]["elements"]) == markdown
    assert feishu_request_body_bytes(build_message_payload(
        open_id="ou_example", msg_type="interactive", content=primary, uuid="om-" + "0" * 32,
    )) <= FEISHU_SEND_REQUEST_BUDGET_BYTES
    assert feishu_request_body_bytes(build_post_message_payload(
        open_id="ou_example", markdown=text, uuid="om-" + "0" * 32 + ":fallback",
    )) <= FEISHU_POST_REQUEST_BUDGET_BYTES
    view = build_daily_brief_user_view(brief, delivery_kind="fixed_report", context=context,
                                      limits={"max_candidates_per_strategy": 5})
    raw = _render_user_view_blocks(view, projection="fixed_report", fit=False)
    cards = [row for row in raw if row.tier == "candidate"]
    shown = [row for row in cards if row in blocks]
    assert 0 < len(shown) < len(cards)
    assert f"{len(cards) - len(shown)} 条开仓卡" in text
    for row in cards:
        title = row.splitlines()[0]
        assert (title in markdown) == (str(row) in markdown)
        assert (title.strip("*") in text) == (row in blocks)
    assert brief == original


def test_real_wheel_and_funds_gaps_cannot_be_removed_by_budget() -> None:
    from tests.test_daily_decision_brief_renderer import _hk_assigned_wheel_brief

    brief = _hk_assigned_wheel_brief()
    brief["funds"] = {"available": False}
    view = build_daily_brief_user_view(brief, delivery_kind="fixed_report", context=_scheduled_context())
    rows = _render_user_view_blocks(view, projection="fixed_report", fit=False)
    warning = next(row for row in rows if "成交归属待人工确认" in row)
    funds = next(row for row in rows if row.startswith("## 资金"))
    assert warning.tier == funds.tier == "gap"
    assert not warning.removable and not funds.removable
    kept, _removed, _hard = _fit_blocks(rows, 100)
    assert any(row is warning for row in kept)
    assert any(row is funds for row in kept)
    assert "CC 覆盖｜待核实" in "\n\n".join(kept)


def test_selection_rejects_oversized_required_gap_without_partial_send() -> None:
    brief, identities = _five_candidate_brief(long=True)
    for row in brief["candidates"]["sell_put"]:
        row["event_risk"] = _render_event_risk("unknown")
    with pytest.raises(ValueError, match="render_unavailable"):
        select_daily_brief_notification(
            brief, delivery_kind="fixed_report", candidate_identities=identities,
            limits={"max_candidates_per_strategy": 5}, context=_scheduled_context(),
            fits=lambda rows, text: feishu_notification_blocks_fit(rows, text, open_id="ou_example"),
        )
