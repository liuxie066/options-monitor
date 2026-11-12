from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _bot(**overrides):
    values = {
        "app_id": "cli_test",
        "app_secret": "secret",
        "user_open_id": "ou_test",
        "send_ready": True,
        "send_missing_fields": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _feishu_success(**kwargs):
    return {
        "ok": True,
        "http_status": 200,
        "request_path": "/open-apis/im/v1/messages?receive_id_type=open_id",
        "feishu_code": 0,
        "message_id": "om_message_1",
        "idempotency_key": kwargs.get("idempotency_key"),
        "effective_idempotency_key": kwargs.get("idempotency_key"),
        "http_attempts": [],
        "fallback_used": False,
        "response_json": {"code": 0, "data": {"message_id": "om_message_1"}},
    }


def _run_args(tmp_path: Path, *extra: str, json_output: bool = True) -> list[str]:
    configs = tmp_path / "configs"
    configs.mkdir(exist_ok=True)
    (configs / "user.hk.json").write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": "0700.HK",
                        "use": ["put_base", "call_base"],
                        "sell_put": {"max_strike": 450},
                        "sell_call": {"enabled": True, "min_strike": 550},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (configs / "user.us.json").write_text(
        json.dumps({"symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 150}}]}),
        encoding="utf-8",
    )
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps({"generated_at_utc": "2026-11-12T00:00:00Z", "prices": {"0700.HK": 403.2, "NVDA": 173.73}}),
        encoding="utf-8",
    )
    return [
        "report",
        "run",
        "config_review",
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--configs-root",
        str(configs),
        "--prices-file",
        str(prices),
        *(["--json"] if json_output else []),
        *extra,
    ]


def test_registry_declares_feishu_delivery_only_for_config_review() -> None:
    from src.application.reports.registry import REPORT_REGISTRY

    deliveries = {item.report_id: item.delivery for item in REPORT_REGISTRY}
    assert deliveries["config_review"] == "feishu_app"
    assert all(value == "none" for report_id, value in deliveries.items() if report_id != "config_review")


def test_delivery_without_channel_is_skipped(tmp_path: Path) -> None:
    from src.application.reports.delivery import deliver_report

    result = deliver_report(
        report_id="config_review",
        run_id="run-1",
        channel="none",
        markdown="# report\n",
        base=tmp_path,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "no_channel"
    assert result["ok"] is False


def test_feishu_delivery_without_credentials_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.reports import delivery

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: True)
    monkeypatch.setattr(
        delivery,
        "resolve_feishu_bot_config",
        lambda: _bot(send_ready=False, send_missing_fields=("OM_FEISHU_BOT_APP_ID", "OM_FEISHU_BOT_USER_OPEN_ID")),
    )
    monkeypatch.setattr(
        delivery,
        "send_feishu_app_message",
        lambda **kwargs: pytest.fail("send must not run without credentials"),
    )

    result = delivery.deliver_report(
        report_id="config_review",
        run_id="run-1",
        channel="feishu_app",
        markdown="# report\n",
        base=tmp_path,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "channel_not_configured"
    assert result["message"] == "未配置渠道，跳过投递"
    assert result["missing"] == ["OM_FEISHU_BOT_APP_ID", "OM_FEISHU_BOT_USER_OPEN_ID"]


def test_feishu_delivery_without_plugin_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.reports import delivery

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: False)

    result = delivery.deliver_report(
        report_id="config_review",
        run_id="run-1",
        channel="feishu_app",
        markdown="# report\n",
        base=tmp_path,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "channel_plugin_not_installed"
    assert "插件未安装" in result["message"]


def test_feishu_delivery_records_confirmed_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.reports import delivery

    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        return _feishu_success(**kwargs)

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: True)
    monkeypatch.setattr(delivery, "resolve_feishu_bot_config", lambda: _bot())
    monkeypatch.setattr(delivery, "send_feishu_app_message", fake_send)

    result = delivery.deliver_report(
        report_id="config_review",
        run_id="run-1",
        channel="feishu_app",
        markdown="# report\n",
        base=tmp_path,
    )

    assert result["status"] == "delivered"
    assert result["ok"] is True
    assert result["message_id"] == "om_message_1"
    assert result["target_reference"].startswith("target:sha256:")
    assert result["idempotency_key"].startswith("om-")
    assert len(result["idempotency_key"]) == 35
    assert len(calls) == 1
    assert calls[0]["target"] == "ou_test"
    assert calls[0]["message"] == "# report\n"
    assert calls[0]["notifications"] is None


def test_feishu_delivery_send_failure_is_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.application.reports import delivery

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: True)
    monkeypatch.setattr(delivery, "resolve_feishu_bot_config", lambda: _bot())
    monkeypatch.setattr(
        delivery,
        "send_feishu_app_message",
        lambda **kwargs: {
            "ok": False,
            "http_status": 500,
            "feishu_code": 19001,
            "request_path": "/open-apis/im/v1/messages",
            "idempotency_key": kwargs.get("idempotency_key"),
            "http_attempts": [],
            "response_json": {"code": 19001, "msg": "server error"},
        },
    )

    result = delivery.deliver_report(
        report_id="config_review",
        run_id="run-1",
        channel="feishu_app",
        markdown="# report\n",
        base=tmp_path,
    )

    assert result["status"] == "failed"
    assert result["ok"] is False
    assert result["error_code"]


def test_report_run_no_deliver_writes_report_without_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.application.reports import delivery
    from src.interfaces.cli.main import main

    monkeypatch.setattr(
        delivery,
        "send_feishu_app_message",
        lambda **kwargs: pytest.fail("--no-deliver must not send"),
    )
    args = _run_args(tmp_path, "--no-deliver")

    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"
    assert payload["delivery"]["status"] == "skipped"
    assert payload["delivery"]["reason"] == "delivery_disabled"
    run_dir = Path(payload["run_dir"])
    assert (run_dir / "report.md").is_file()
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["delivery"]["reason"] == "delivery_disabled"


def test_report_run_delivers_and_records_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.application.reports import delivery
    from src.interfaces.cli.main import main

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: True)
    monkeypatch.setattr(delivery, "resolve_feishu_bot_config", lambda: _bot())
    monkeypatch.setattr(delivery, "send_feishu_app_message", lambda **kwargs: _feishu_success(**kwargs))

    assert main(_run_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery"]["status"] == "delivered"
    assert payload["delivery"]["message_id"] == "om_message_1"
    run_dir = Path(payload["run_dir"])
    assert (run_dir / "report.md").is_file()
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"
    assert manifest["delivery"]["status"] == "delivered"


def test_report_run_delivery_failure_json_mode_keeps_report_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.application.reports import delivery
    from src.interfaces.cli.main import main

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: True)
    monkeypatch.setattr(delivery, "resolve_feishu_bot_config", lambda: _bot())
    monkeypatch.setattr(
        delivery,
        "send_feishu_app_message",
        lambda **kwargs: {
            "ok": False,
            "http_status": 500,
            "feishu_code": 19001,
            "request_path": "/open-apis/im/v1/messages",
            "idempotency_key": kwargs.get("idempotency_key"),
            "http_attempts": [],
            "response_json": {"code": 19001, "msg": "server error"},
        },
    )

    assert main(_run_args(tmp_path)) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["status"] == "succeeded"
    assert payload["delivery"]["status"] == "failed"
    assert (Path(payload["run_dir"]) / "report.md").is_file()
    assert "投递失败" in captured.err


def test_report_dry_run_never_delivers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.application.reports import delivery
    from src.interfaces.cli.main import main

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: True)
    monkeypatch.setattr(delivery, "resolve_feishu_bot_config", lambda: _bot())
    monkeypatch.setattr(
        delivery,
        "send_feishu_app_message",
        lambda **kwargs: pytest.fail("--dry-run must not send"),
    )

    assert main(_run_args(tmp_path, "--dry-run")) == 0
    capsys.readouterr()
    assert not (tmp_path / "runtime" / "reports").exists()


def test_report_run_delivery_failure_text_mode_keeps_report_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.application.reports import delivery
    from src.interfaces.cli.main import main

    monkeypatch.setattr(delivery, "channel_plugin_installed", lambda channel: True)
    monkeypatch.setattr(delivery, "resolve_feishu_bot_config", lambda: _bot())
    monkeypatch.setattr(
        delivery,
        "send_feishu_app_message",
        lambda **kwargs: {
            "ok": False,
            "http_status": 500,
            "feishu_code": 19001,
            "request_path": "/open-apis/im/v1/messages",
            "idempotency_key": kwargs.get("idempotency_key"),
            "http_attempts": [],
            "response_json": {"code": 19001, "msg": "server error"},
        },
    )

    assert main(_run_args(tmp_path, json_output=False)) == 1
    captured = capsys.readouterr()
    assert "投递失败" in captured.err
    reports = list((tmp_path / "runtime" / "reports" / "config_review").glob("*/report.md"))
    assert len(reports) == 1
    assert "运行成功" in captured.out
    assert "feishu_app 投递失败" in captured.out

