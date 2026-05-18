from __future__ import annotations

import sys
from pathlib import Path

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

from src.application.option_intake import _missing_for_action, parse_om_command
from src.application.parse_option_message import parse_futu_premium


def test_parse_om_open_command_with_review_and_account() -> None:
    cmd = parse_om_command("/om -r -lx open 【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93")

    assert cmd.action == "open"
    assert cmd.account == "lx"
    assert cmd.dry_run is True
    assert cmd.apply is False
    assert cmd.record_id is None
    assert cmd.text.startswith("【成交提醒】成功卖出2张")


def test_parse_om_open_command_with_canonical_format_and_separator() -> None:
    cmd = parse_om_command("/om open lx -r -- 【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93")

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


def test_parse_om_close_command_with_compact_account_and_record_id() -> None:
    cmd = parse_om_command("/om close sy id:rec123 -a -- 【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20")

    assert cmd.action == "close"
    assert cmd.account == "sy"
    assert cmd.apply is True
    assert cmd.dry_run is False
    assert cmd.record_id == "rec123"
    assert "成功买入1张" in cmd.text


def test_parse_om_close_command_accepts_btc_and_bare_record_id() -> None:
    cmd = parse_om_command("/om btc sy rec123 -r -- 【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交均价：1.20")

    assert cmd.action == "close"
    assert cmd.account == "sy"
    assert cmd.dry_run is True
    assert cmd.apply is False
    assert cmd.record_id == "rec123"
    assert "成交均价" in cmd.text


def test_parse_om_close_command_accepts_buy_to_close_phrase() -> None:
    cmd = parse_om_command("/om buy to close lx lot_manual-open-1 -a -- 【成交提醒】成功买入1张$NVDA 260618 154.00P$，成交价：1.20")

    assert cmd.action == "close"
    assert cmd.account == "lx"
    assert cmd.apply is True
    assert cmd.dry_run is False
    assert cmd.record_id == "lot_manual-open-1"
    assert "成功买入1张" in cmd.text


def test_parse_om_close_command_accepts_chinese_close_alias() -> None:
    cmd = parse_om_command("/om 买平 sy rec123 检查 -- 【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20")

    assert cmd.action == "close"
    assert cmd.account == "sy"
    assert cmd.dry_run is True
    assert cmd.apply is False
    assert cmd.record_id == "rec123"
    assert "成功买入1张" in cmd.text


def test_parse_futu_premium_accepts_close_price_aliases() -> None:
    assert parse_futu_premium("【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20") == 1.2
    assert parse_futu_premium("【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价：1.21") == 1.21
    assert parse_futu_premium("【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交均价：1.22") == 1.22
    assert parse_futu_premium("【成交提醒】成功买入1张$腾讯 260429 480.00 购$，成交价格：1.235") == 1.235


def test_open_action_reports_missing_premium() -> None:
    from src.application.parse_option_message import parse_option_message_text

    parsed = parse_option_message_text(
        "lx USD 【成交提醒】成功买入1张$NVDA 260619 120.00C$",
        resolve_multiplier=False,
    )

    assert parsed["ok"] is False
    assert "premium_per_share" in parsed["missing"]


def test_parse_om_command_supports_generic_at_account() -> None:
    cmd = parse_om_command("/om open @alpha review -- 【成交提醒】成功卖出1张$NVDA 260618 154.00P$，成交价格：2.20")

    assert cmd.action == "open"
    assert cmd.account == "alpha"
    assert cmd.dry_run is True
    assert cmd.apply is False
    assert cmd.text.startswith("【成交提醒】成功卖出1张")


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
    src = Path(__file__).resolve().parents[1] / "src" / "application" / "option_intake.py"
    text = src.read_text(encoding="utf-8")

    assert "import subprocess" not in text
    assert "from src.application.parse_option_message import parse_option_message_text" in text
    assert "scripts/option_positions.py" not in text


def test_option_intake_close_auto_matches_unique_selector(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.application.option_intake as intake
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=2,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]

    monkeypatch.setattr(intake, "open_position_ledger_from_data_config", lambda **_kwargs: (tmp_path / "data.json", repo))
    monkeypatch.setattr(
        intake,
        "parse_option_message_text",
        lambda *_args, **_kwargs: {
            "ok": True,
            "raw": "close",
            "missing": [],
            "parsed": {
                "account": "lx",
                "symbol": "0700.HK",
                "option_type": "put",
                "side": "long",
                "strike": 480.0,
                "exp": "2026-04-29",
                "premium_per_share": 1.2,
                "contracts": 1,
                "currency": "HKD",
                "market": "富途",
            },
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "option_intake",
            "--text",
            "【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20",
            "--action",
            "close",
            "--apply",
        ],
    )

    assert intake.main() == 0

    out = capsys.readouterr().out
    assert f"[MATCH] rule=strict_contract_unique record_id={lot['record_id']}" in out
    assert f"[DONE] buy-closed {lot['record_id']} contracts=1" in out
    assert repo.get_record_fields(lot["record_id"])["contracts_open"] == 1


def test_option_intake_close_parser_skips_multiplier_resolution(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.application.option_intake as intake
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(intake, "open_position_ledger_from_data_config", lambda **_kwargs: (tmp_path / "data.json", repo))

    def _fake_parse(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "ok": False,
            "raw": "close",
            "missing": ["multiplier"],
            "parsed": {
                "account": "lx",
                "symbol": "0700.HK",
                "option_type": "put",
                "side": "long",
                "strike": 480.0,
                "exp": "2026-04-29",
                "premium_per_share": 1.2,
                "contracts": 1,
                "currency": "HKD",
                "market": "富途",
                "multiplier": None,
            },
        }

    monkeypatch.setattr(intake, "parse_option_message_text", _fake_parse)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "option_intake",
            "--text",
            "【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20",
            "--action",
            "close",
            "--dry-run",
        ],
    )

    assert intake.main() == 0

    assert captured["resolve_multiplier"] is False
    assert "[DRY_RUN] update fields:" in capsys.readouterr().out
