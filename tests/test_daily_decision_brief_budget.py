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
    _strategy_partial_data_gap,
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
    assert "CSP 本轮 5 个候选，入选 3 个，另有 2 个未展开" in selection["text"]
    no_summary = [row for row in blocks if not row.summary_family]
    evidence = _display_evidence(brief, identities, blocks, no_summary)
    assert [row["state"] for row in evidence] == [
        "expanded", "expanded", "expanded", "not_shown", "not_shown",
    ]
    assert _display_evidence({}, ["position-roll:v1:lx:US:roll:id"], [],
                             [_Block("另有 1 条", "omission", summary_family="all")])[0]["state"] == "not_shown"


@pytest.mark.parametrize("delivery_kind", ["candidate_alert", "fixed_report"])
@pytest.mark.parametrize("long", [False, True])
def test_partial_range_cards_keep_coverage_and_display_evidence_for_surviving_cards(delivery_kind, long) -> None:
    brief, identities = _five_candidate_brief(long=long)
    for row in brief["candidates"]["sell_put"]:
        strike, expiration = row["strike"], row["expiration"]
        scope = {
            "request_index": 0, "option_type": "put", "status": "incomplete",
            "strike_window": [strike - 10, strike + 10],
            "covered_strikes": [strike - 5, strike, strike + 5],
            "covered_ranges": [[strike - 5, strike + 5]],
            "missing_ranges": [[strike - 10, strike - 5], [strike + 5, strike + 10]],
            "missing_range_inclusive": [[True, False], [False, True]],
        }
        coverage = {
            "status": "partial", "reason_code": "required_data_partial_coverage",
            "covered_expirations": [], "incomplete_expirations": [expiration],
            "ranking_complete": False, "ranking_scope": "observed_contracts",
            "partial_candidate_policy": "retain_and_mark",
            "per_expiration": [{"expiration": expiration, **scope, "strike_coverage": [scope]}],
        }
        row.update(required_data_coverage=coverage, required_data_expiration_status="incomplete",
                   ranking_complete=False, ranking_scope="observed_contracts",
                   partial_candidate_policy="retain_and_mark")
    # A surviving gap summary mentions NVDA even when its whole candidate card
    # is removed. That mention must not upgrade the frozen display evidence.
    brief["data_gaps"] = [{**_strategy_partial_data_gap("NVDA"),
        "reason_code": "required_data_partial_coverage",
        "required_data_coverage": brief["candidates"]["sell_put"][1]["required_data_coverage"]}]
    selection = select_daily_brief_notification(
        brief, delivery_kind=delivery_kind, candidate_identities=identities,
        limits={"max_candidates_per_strategy": 5} if long else None,
        context=_scheduled_context(),
        fits=lambda rows, text: len(text) <= (4000 if long else 12_000)
        and feishu_notification_blocks_fit(rows, text, open_id="ou_example"),
    )
    # A smaller recipient budget forces two originally selected long cards out.
    expected = (["expanded"] + ["not_shown"] * 4
                if long else ["expanded"] * 3 + ["summary_notified"] * 2)
    assert [row["state"] for row in selection["display_evidence"]] == expected
    cards = [block for block in selection["markdown_blocks"] if block.tier == "candidate" and block.identities]
    assert len(cards) == (1 if long else 3)
    assert "NVDA CSP" in selection["text"]
    for block in cards:
        for content in (str(block), block.plain_text):
            assert "覆盖不完整" in content
            assert "已观测 [" in content and "缺口 [" in content
            assert ")、(" in content
            assert "仅对已观测合约排序，未观测行权价可能改变排序" in content
    envelope = render_feishu_notification_card(
        markdown="\n\n".join(selection["markdown_blocks"]), markdown_blocks=selection["markdown_blocks"],
        fallback_text=selection["text"], fallback_open_id="ou_example", preserve_blocks=True,
    )
    assert not envelope["render_meta"]["truncated"]
    assert envelope["fallback"]["markdown"] == envelope["text"] == selection["text"]
    primary = envelope["transport"]["content"]
    markdown = "\n\n".join(row["content"] for row in primary["body"]["elements"])
    assert markdown == "\n\n".join(selection["markdown_blocks"])
    assert feishu_notification_blocks_fit(selection["markdown_blocks"], selection["text"], open_id="ou_example")
    for block in cards:
        assert str(block) in markdown
        assert block.plain_text in envelope["text"]


def test_channel_fitting_preserves_holding_round_robin_priority_and_exact_roll_ids() -> None:
    from domain.domain.daily_decision_brief import daily_brief_position_roll_identity
    from test_daily_decision_brief_selection import _close, _roll, _source
    up, down = _roll(0), _roll(1, option_type="put")
    brief = _source([_close(0), up, down])
    identities = [daily_brief_position_roll_identity(row, account=brief["account"], market=brief["market"])
                  for row in (up, down)]
    result = select_daily_brief_notification(brief, delivery_kind="fixed_report", candidate_identities=identities,
        context={"advice_time_utc": "2026-07-21T14:00:00Z"},
        fits=lambda blocks, text: sum(row.tier == "action" for row in blocks) <= 2)
    actions = [row for row in result["markdown_blocks"] if row.tier == "action"]
    assert len(actions) == 2
    assert "C0" in str(actions) and "R0" in str(actions)
    assert "R1" not in str(actions)
    assert {row["identity"]: row["state"] for row in result["display_evidence"]} == {
        identities[0]: "expanded", identities[1]: "not_shown",
    }


def test_channel_fitting_uses_actual_card_deadline_over_close_group_deadline() -> None:
    from domain.domain.daily_decision_brief import daily_brief_position_roll_identity
    from test_daily_decision_brief_selection import _urgent_roll_and_close_remainder_brief

    brief = _urgent_roll_and_close_remainder_brief()
    identity = daily_brief_position_roll_identity(brief["positions"][0],
        account=brief["account"], market=brief["market"])
    result = select_daily_brief_notification(brief, delivery_kind="fixed_report", candidate_identities=[identity],
        context={"advice_time_utc": "2026-07-20T14:04:00Z", "next_fixed_report_at_utc": "2026-07-20T15:00:00Z"},
        fits=lambda blocks, text: sum(row.tier == "action" for row in blocks) <= 1)
    assert {row["identity"]: row["state"] for row in result["display_evidence"]} == {identity: "expanded"}
    assert "CC 向上移仓" in result["text"]
    assert sum(row.tier == "action" for row in result["markdown_blocks"]) == 1


def test_cross_target_close_group_fitting_keeps_down_turn_and_exact_evidence() -> None:
    from domain.domain.daily_decision_brief import daily_brief_position_roll_identity
    from test_daily_decision_brief_selection import _cross_target_close_brief

    brief = _cross_target_close_brief()
    identities = [daily_brief_position_roll_identity(row, account=brief["account"], market=brief["market"])
                  for row in brief["positions"] if row.get("roll_advice")]
    result = select_daily_brief_notification(brief, delivery_kind="fixed_report", candidate_identities=identities,
        context={"advice_time_utc": "2026-07-20T14:04:00Z"},
        fits=lambda blocks, text: sum(row.tier == "action" for row in blocks) <= 2)
    states = {row["identity"]: row["state"] for row in result["display_evidence"]}
    assert [states[identity] for identity in identities] == ["expanded", "not_shown", "not_shown", "expanded"]
    assert "CC 向上移仓" in result["text"] and "CSP 向下移仓" in result["text"]
    assert sum(row.tier == "action" for row in result["markdown_blocks"]) == 2


def test_wheel_evidence_uses_selected_branch_after_total_budget() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity
    brief = _brief()
    brief.update(positions=[], candidates={"wheel": []}, candidate_index=[], wheel_batches=[])
    identities = []
    for index, rank in ((0, 2), (1, 1)):
        branch = f"branch-{index}"
        row = _candidate(rank=rank, symbol=f"W{index}", option_type="put",
                         expiration="2026-08-21", strike=100, capacity=1)
        row.update(wheel_branch_id=branch, direction="put", strategy_family="wheel")
        brief["candidates"]["wheel"].append(row)
        brief["wheel_batches"].append({"wheel_branch_id": branch, "symbol": f"W{index}",
            "direction": "put", "remaining_contracts": 1, "recommended_contracts": 1,
            "status": "ready", "expiration": "2026-08-21", "strike": 100, "sell_limit": 1.2})
        identity = build_daily_brief_candidate_identity(account="lx", market="US", symbol=f"W{index}",
            strategy_family="wheel", wheel_branch_id=branch)
        identities.append(identity)
        brief["candidate_index"].append({"identity": identity, "symbol": f"W{index}",
            "strategy_family": "wheel", "representative": row})
    selected = select_daily_brief_notification(brief, delivery_kind="fixed_report",
        candidate_identities=identities, limits={"max_total_items": 1}, context=_scheduled_context())
    assert "W1｜Wheel" in selected["text"]
    states = {row["identity"]: row["state"] for row in selected["display_evidence"]}
    assert states[identities[1]] == "expanded"
    assert states[identities[0]] != "expanded"


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
