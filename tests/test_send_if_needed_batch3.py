from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _argv(cfg_path: Path) -> list[str]:
    return [
        "send_if_needed.py",
        "--config",
        str(cfg_path),
        "--state-dir",
        "output/state_test_send_if_needed_batch3",
        "--target",
        "user:test",
        "--notification",
        "README.md",
    ]


def test_send_if_needed_scheduler_view_compat_should_notify_field(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    calls = send_if_needed_common_patches["calls"]

    monkeypatch.setattr(
        mod,
        "run_scan_scheduler_cli",
        lambda **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "should_run_scan": True,
                    "should_notify": True,
                    "reason": "compat",
                }
            ),
            stderr="",
        )
        if not kwargs.get("mark_notified")
        else SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    argv_scope(_argv(example_config_path))
    rc = mod.main()
    assert rc == 0
    assert any("sent" in " ".join(cmd) for cmd, _ in calls)


def test_send_if_needed_scheduler_view_prefers_is_notify_window_open_field(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    calls = send_if_needed_common_patches["calls"]
    scheduler_calls: list[dict] = []
    send_calls = {"n": 0}

    def _scheduler(**kwargs):
        scheduler_calls.append(dict(kwargs))
        if kwargs.get("mark_notified"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "should_run_scan": True,
                    "is_notify_window_open": False,
                    "should_notify": True,
                    "reason": "prefer-new-field",
                }
            ),
            stderr="",
        )

    def _send(**_kwargs):
        send_calls["n"] += 1
        return SimpleNamespace(returncode=0, stdout='{"messageId":"m1"}', stderr="")

    monkeypatch.setattr(mod, "run_scan_scheduler_cli", _scheduler)
    monkeypatch.setattr(mod, "send_openclaw_message", _send)

    argv_scope(_argv(example_config_path))
    rc = mod.main()
    assert rc == 0
    assert send_calls["n"] == 0
    assert any("should_notify=False" in " ".join(cmd) for cmd, _ in calls)
    assert not any(bool(item.get("mark_notified")) for item in scheduler_calls)


def test_send_if_needed_uses_normalized_notify_message_id_from_nested_payload(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    calls = send_if_needed_common_patches["calls"]

    monkeypatch.setattr(
        mod,
        "run_scan_scheduler_cli",
        lambda **kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")
        if kwargs.get("mark_notified")
        else SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "should_run_scan": True,
                    "is_notify_window_open": True,
                    "reason": "ok",
                }
            ),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        mod,
        "send_openclaw_message",
        lambda **_kwargs: SimpleNamespace(returncode=0, stdout='{"result":{"messageId":"nested-42"}}', stderr=""),
    )

    argv_scope(_argv(example_config_path))
    rc = mod.main()
    assert rc == 0
    assert any("message_id=nested-42" in " ".join(cmd) for cmd, _ in calls)


def test_send_if_needed_treats_returncode_zero_without_message_id_as_unconfirmed(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    calls = send_if_needed_common_patches["calls"]
    mark_calls = {"n": 0}

    def _scheduler(**kwargs):
        if kwargs.get("mark_notified"):
            mark_calls["n"] += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "should_run_scan": True,
                    "is_notify_window_open": True,
                    "reason": "ok",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(mod, "run_scan_scheduler_cli", _scheduler)
    monkeypatch.setattr(mod, "send_openclaw_message", lambda **_kwargs: SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr=""))

    argv_scope(_argv(example_config_path))
    rc = mod.main()
    assert rc == 1
    assert mark_calls["n"] == 0
    assert any("SEND_UNCONFIRMED" in " ".join(cmd) for cmd, _ in calls)
    assert any("message_id is missing" in " ".join(cmd) for cmd, _ in calls)


def test_send_if_needed_trading_day_guard_market_inference_delegates_to_domain(send_if_needed_module, monkeypatch) -> None:
    mod = send_if_needed_module
    seen: dict[str, object] = {}

    def _fake(markets_to_run, cfg, market_config):
        seen["markets_to_run"] = list(markets_to_run or [])
        seen["cfg"] = cfg
        seen["market_config"] = market_config
        return ["HK"]

    monkeypatch.setattr(mod, "domain_markets_for_trading_day_guard", _fake)
    cfg = {"symbols": [{"symbol": "0700.HK", "market": "HK"}]}
    out = mod._infer_trading_day_guard_markets(cfg)
    assert out == ["HK"]
    assert seen["markets_to_run"] == []
    assert seen["cfg"] is cfg
    assert seen["market_config"] == "auto"


def test_send_if_needed_uses_feishu_app_sender(
    argv_scope,
    example_config_path,
    monkeypatch,
    send_if_needed_common_patches,
) -> None:
    mod = send_if_needed_common_patches["module"]
    calls = send_if_needed_common_patches["calls"]
    send_seen: list[dict] = []

    monkeypatch.setattr(
        mod,
        "run_scan_scheduler_cli",
        lambda **kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")
        if kwargs.get("mark_notified")
        else SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": True, "reason": "ok"}),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        mod,
        "send_feishu_app_message_process",
        lambda **kwargs: send_seen.append(kwargs) or SimpleNamespace(returncode=0, stdout="", stderr="", raw={"http_status": 200, "response_json": {"code": 0, "data": {"message_id": "m-feishu"}}}),
    )
    monkeypatch.setattr(mod, "send_openclaw_message", mod._DEFAULT_OPENCLAW_SENDER)
    monkeypatch.setattr(mod, "normalize_notify_subprocess_output", mod._DEFAULT_NOTIFY_NORMALIZER)
    monkeypatch.setattr(
        mod,
        "normalize_feishu_app_send_output",
        lambda *, send_result: {"ok": True, "command_ok": True, "delivery_confirmed": True, "message_id": "m-feishu", "returncode": 0},
    )

    argv_scope(_argv(example_config_path))
    rc = mod.main()
    assert rc == 0
    assert len(send_seen) == 1
    assert send_seen[0]["channel"] == "feishu"
    assert send_seen[0]["target"] == "user:test"
    assert any("message_id=m-feishu" in " ".join(cmd) for cmd, _ in calls)
