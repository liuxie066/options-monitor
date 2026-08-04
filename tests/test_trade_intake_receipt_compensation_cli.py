from __future__ import annotations

import json
from pathlib import Path

import src.application.trades.auto_intake as auto_intake
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.run_ops import _trade_intake_argv


DEAL_ID = "futu:lx:100000000000000001:2000000000000000001"
PAYLOAD_HASH = "a" * 64


def test_run_cli_forwards_receipt_compensation_arguments() -> None:
    args = parse_args(
        [
            "run",
            "trade-intake",
            "--config",
            "config.us.json",
            "--runtime-root",
            "/var/lib/options-monitor",
            "--compensate-receipts",
            "--account",
            "lx",
            "--deal-id",
            DEAL_ID,
            "--expected-payload-hash",
            PAYLOAD_HASH,
            "--apply",
            "--confirm",
        ]
    )

    assert _trade_intake_argv(args) == [
        "--config",
        "config.us.json",
        "--runtime-root",
        "/var/lib/options-monitor",
        "--confirm",
        "--compensate-receipts",
        "--compensation-reason",
        "legacy_false_outbox_marker",
        "--account",
        "lx",
        "--deal-id",
        DEAL_ID,
        "--expected-payload-hash",
        PAYLOAD_HASH,
        "--apply",
    ]


def test_compensation_apply_requires_explicit_confirmation(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(auto_intake, "load_config", lambda **_kwargs: {})
    monkeypatch.setattr(
        auto_intake,
        "resolve_trade_intake_config",
        lambda *_args, **_kwargs: {
            "mode": "apply",
            "enabled": True,
            "state_path": Path("state.json"),
            "audit_path": Path("audit.jsonl"),
            "status_path": Path("status.json"),
            "receipt": {"enabled": True},
            "backfill": {},
            "holdings_sync": {},
            "account_mapping": {},
            "futu_account_ids": [],
            "sources": [
                {
                    "id": "lx",
                    "account": "lx",
                    "state_path": Path("state/lx.json"),
                    "audit_path": Path("audit/lx.jsonl"),
                    "status_path": Path("status/lx.json"),
                    "receipt": {"enabled": True},
                }
            ],
        },
    )

    rc = auto_intake.main(
        [
            "--config",
            str(config_path),
            "--runtime-root",
            str(tmp_path),
            "--compensate-receipts",
            "--account",
            "lx",
            "--deal-id",
            DEAL_ID,
            "--expected-payload-hash",
            PAYLOAD_HASH,
            "--apply",
        ]
    )

    assert rc == 2
    assert "use --apply with --confirm or --yes" in capsys.readouterr().out

    rc = auto_intake.main(
        [
            "--config",
            str(config_path),
            "--runtime-root",
            str(tmp_path),
            "--compensate-receipts",
            "--account",
            "lx",
            "--deal-id",
            DEAL_ID,
            "--apply",
            "--confirm",
        ]
    )

    assert rc == 2
    assert "requires --expected-payload-hash" in capsys.readouterr().out


def test_compensation_dry_run_routes_to_guarded_application_service(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    source = {
        "id": "lx",
        "account": "lx",
        "state_path": Path("state/lx.json"),
        "audit_path": Path("audit/lx.jsonl"),
        "status_path": Path("status/lx.json"),
        "receipt": {"enabled": True},
    }
    monkeypatch.setattr(auto_intake, "load_config", lambda **_kwargs: {})
    monkeypatch.setattr(
        auto_intake,
        "resolve_trade_intake_config",
        lambda *_args, **_kwargs: {
            "mode": "apply",
            "enabled": True,
            "state_path": Path("state.json"),
            "audit_path": Path("audit.jsonl"),
            "status_path": Path("status.json"),
            "receipt": {"enabled": True},
            "backfill": {},
            "holdings_sync": {},
            "account_mapping": {},
            "futu_account_ids": [],
            "sources": [source],
        },
    )
    monkeypatch.setattr(
        auto_intake,
        "open_position_ledger_from_runtime_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )
    captured = {}

    def _compensate(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "ready",
            "dry_run": True,
            "write_applied": False,
            "message": "preview",
        }

    monkeypatch.setattr(
        auto_intake,
        "compensate_trade_intake_receipts",
        _compensate,
    )

    rc = auto_intake.main(
        [
            "--config",
            str(config_path),
            "--runtime-root",
            str(tmp_path),
            "--compensate-receipts",
            "--account",
            "lx",
            "--deal-id",
            DEAL_ID,
            "--dry-run",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "ready"
    assert payload["dry_run"] is True
    assert captured["account"] == "lx"
    assert captured["deal_ids"] == [DEAL_ID]
    assert captured["apply_changes"] is False
    assert captured["expected_payload_hash"] is None
    assert Path(captured["sources"][0]["state_path"]) == (
        tmp_path / "state/lx.json"
    ).resolve()
