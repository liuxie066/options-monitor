from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

from src.application.option_intake import _missing_for_action, parse_om_command
from src.application.parse_option_message import parse_fill_timestamp, parse_futu_premium


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


def _seed_popmart_short_call(repo: ledger_repository.SQLiteOptionPositionsRepository) -> str:
    from domain.domain.option_position_lots import OpenPositionCommand

    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="sy",
            symbol="9992.HK",
            option_type="call",
            side="short",
            contracts=3,
            currency="HKD",
            strike=200.0,
            multiplier=100,
            expiration_ymd="2026-06-29",
            premium_per_share=1.5,
            opened_at_ms=1000,
        ),
    )
    return str(repo.list_position_lots()[0]["record_id"])


def _btc_popmart_message() -> str:
    return (
        "/om btc sy -- 【成交提醒】成功买入3张$泡泡玛特 260629 200.00 购$，"
        "成交价格：0.72，此笔订单委托已全部成交，2026/05/19 10:42:31 (香港)。"
        "【富途证券(香港)】"
    )


def _fake_open_parse() -> dict[str, object]:
    return {
        "ok": True,
        "raw": "open",
        "missing": [],
        "parsed": {
            "account": "sy",
            "symbol": "9992.HK",
            "option_type": "call",
            "side": "short",
            "strike": 200.0,
            "exp": "2026-06-29",
            "premium_per_share": 1.5,
            "contracts": 3,
            "currency": "HKD",
            "market": "富途",
            "multiplier": 100,
        },
    }


def test_option_intake_runtime_config_path_resolves_runtime_ledger_without_env(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.application.option_intake as intake
    import src.application.ledger.store_resolution as store_resolution

    release_root = tmp_path / "apps" / "releases" / "1.2.74"
    runtime_root = tmp_path / "var" / "lib" / "options-monitor"
    release_root.mkdir(parents=True)
    runtime_root.mkdir(parents=True)
    config_path = runtime_root / "config.hk.json"
    config_path.write_text(json.dumps({"accounts": ["sy"], "portfolio": {}}, ensure_ascii=False), encoding="utf-8")

    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(intake, "repo_base", release_root)
    monkeypatch.setattr(store_resolution, "REPO_BASE", release_root)
    monkeypatch.setattr(intake, "load_config", lambda **_kwargs: {"accounts": ["sy"], "portfolio": {}})
    monkeypatch.setattr(intake, "parse_option_message_text", lambda *_args, **_kwargs: _fake_open_parse())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "option_intake",
            "--config",
            str(config_path),
            "--text",
            "/om -sy open 成交提醒",
            "--apply",
        ],
    )

    assert intake.main() == 0

    active_db = runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    release_local_db = release_root / "output_shared" / "state" / "option_positions.sqlite3"
    assert active_db.exists()
    assert not release_local_db.exists()
    repo = ledger_repository.SQLiteOptionPositionsRepository(active_db)
    assert repo.count_trade_events() == 1
    store_inspect = store_resolution.inspect_ledger_stores(runtime_root / "portfolio.runtime.json", config_path=config_path)
    assert store_inspect["summary"]["multiple_populated"] is False
    assert not any("multiple ledger sqlite candidates" in item for item in store_inspect["warnings"])
    out = capsys.readouterr().out
    assert f"[LEDGER] sqlite={active_db.resolve()}" in out
    assert "[DONE] created event_id=" in out


def test_option_intake_apply_fails_closed_when_release_local_store_has_rows(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import src.application.option_intake as intake
    import src.application.ledger.store_resolution as store_resolution

    release_root = tmp_path / "apps" / "releases" / "1.2.74"
    runtime_root = tmp_path / "var" / "lib" / "options-monitor"
    release_root.mkdir(parents=True)
    runtime_root.mkdir(parents=True)
    config_path = runtime_root / "config.hk.json"
    config_path.write_text(json.dumps({"accounts": ["sy"], "portfolio": {}}, ensure_ascii=False), encoding="utf-8")

    release_local_db = release_root / "output_shared" / "state" / "option_positions.sqlite3"
    release_repo = ledger_repository.SQLiteOptionPositionsRepository(release_local_db)
    _seed_popmart_short_call(release_repo)

    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(intake, "repo_base", release_root)
    monkeypatch.setattr(store_resolution, "REPO_BASE", release_root)
    monkeypatch.setattr(intake, "load_config", lambda **_kwargs: {"accounts": ["sy"], "portfolio": {}})
    monkeypatch.setattr(intake, "parse_option_message_text", lambda *_args, **_kwargs: _fake_open_parse())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "option_intake",
            "--config",
            str(config_path),
            "--text",
            "/om -sy open 成交提醒",
            "--apply",
        ],
    )

    assert intake.main() == 2

    active_db = runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    assert not active_db.exists()
    out = capsys.readouterr().out
    assert "[LEDGER_WARN] active ledger sqlite has no rows while another candidate is populated" in out
    assert "[LEDGER_FAIL] divergent populated ledger stores detected; aborting apply" in out


def test_option_intake_btc_dry_run_uses_parsed_fill_timestamp(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.application.option_intake as intake

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    lot_id = _seed_popmart_short_call(repo)
    monkeypatch.setattr(intake, "open_position_ledger_from_data_config", lambda **_kwargs: (tmp_path / "data.json", repo))
    message = _btc_popmart_message()
    expected_ms = parse_fill_timestamp(message)
    assert expected_ms is not None

    monkeypatch.setattr(sys, "argv", ["option_intake", "--text", message, "--dry-run"])

    assert intake.main() == 0

    out = capsys.readouterr().out
    assert f"[MATCH] rule=strict_contract_unique record_id={lot_id}" in out
    assert f'"closed_at": {expected_ms}' in out
    assert f'"last_action_at": {expected_ms}' in out
    assert '"closed_at_beijing": "2026-05-19 10:42:31 北京时间"' in out
    assert '"last_action_at_beijing": "2026-05-19 10:42:31 北京时间"' in out
    assert repo.get_record_fields(lot_id)["status"] == "open"
    assert len(repo.list_trade_events()) == 1


def test_option_intake_btc_apply_uses_parsed_fill_timestamp(monkeypatch, tmp_path: Path, capsys) -> None:
    import src.application.option_intake as intake

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    lot_id = _seed_popmart_short_call(repo)
    monkeypatch.setattr(intake, "open_position_ledger_from_data_config", lambda **_kwargs: (tmp_path / "data.json", repo))
    message = _btc_popmart_message()
    expected_ms = parse_fill_timestamp(message)
    assert expected_ms is not None
    expected_dt = datetime.fromtimestamp(expected_ms / 1000, tz=timezone.utc)
    assert (expected_dt.hour, expected_dt.minute, expected_dt.second) == (2, 42, 31)

    monkeypatch.setattr(sys, "argv", ["option_intake", "--text", message, "--apply"])

    assert intake.main() == 0

    out = capsys.readouterr().out
    assert "成交时间=2026-05-19 10:42:31 北京时间" in out
    fields = repo.get_record_fields(lot_id)
    assert fields["status"] == "close"
    assert fields["closed_at"] == expected_ms
    assert fields["last_action_at"] == expected_ms
    close_event = repo.list_trade_events()[-1]
    assert close_event["event_type"] == "close"
    assert close_event["trade_time_ms"] == expected_ms
