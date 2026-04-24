from __future__ import annotations

from pathlib import Path

from scripts.option_intake import _missing_for_action, parse_om_command


def test_parse_om_open_command_with_review_and_account() -> None:
    cmd = parse_om_command("/om -r -lx open 【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93")

    assert cmd.action == "open"
    assert cmd.account == "lx"
    assert cmd.dry_run is True
    assert cmd.apply is False
    assert cmd.record_id is None
    assert cmd.text.startswith("【成交提醒】成功卖出2张")


def test_parse_om_close_command_with_record_id() -> None:
    cmd = parse_om_command("/om --apply --account sy close --record-id rec123 【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20")

    assert cmd.action == "close"
    assert cmd.account == "sy"
    assert cmd.apply is True
    assert cmd.dry_run is False
    assert cmd.record_id == "rec123"
    assert "成功买入1张" in cmd.text


def test_parse_plain_text_is_not_treated_as_command() -> None:
    raw = "【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93"
    cmd = parse_om_command(raw)

    assert cmd.action is None
    assert cmd.account is None
    assert cmd.text == raw


def test_close_action_requires_only_close_fields_from_parsed_message() -> None:
    parsed = {
        "ok": False,
        "missing": ["multiplier"],
        "parsed": {
            "contracts": 1,
            "account": "lx",
            "premium_per_share": 1.2,
        },
    }

    assert _missing_for_action(parsed, "close") == []


def test_close_action_reports_missing_close_fields() -> None:
    parsed = {
        "ok": False,
        "missing": ["multiplier", "account"],
        "parsed": {
            "contracts": 1,
            "account": None,
            "premium_per_share": None,
        },
    }

    assert _missing_for_action(parsed, "close") == ["account", "close_price"]


def test_option_intake_no_longer_shells_out_to_parser_or_option_positions() -> None:
    src = Path(__file__).resolve().parents[1] / "scripts" / "option_intake.py"
    text = src.read_text(encoding="utf-8")

    assert "import subprocess" not in text
    assert "from scripts.parse_option_message import parse_option_message_text" in text
    assert "scripts/option_positions.py" not in text
