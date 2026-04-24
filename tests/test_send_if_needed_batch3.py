from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _test_config_path() -> Path:
    local = BASE / "config.us.json"
    if local.exists():
        return local.resolve()
    return (BASE / "configs" / "examples" / "config.example.us.json").resolve()


def test_send_if_needed_scheduler_view_compat_should_notify_field() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    cfg_path = _test_config_path()
    old = {
        "argv": sys.argv[:],
        "acquire_lock": mod._acquire_lock,
        "release_lock": mod._release_lock,
        "run_scan_scheduler_cli": mod.run_scan_scheduler_cli,
        "run_pipeline_script": mod.run_pipeline_script,
        "send_openclaw_message": mod.send_openclaw_message,
        "trading_day_via_futu": mod.trading_day_via_futu,
        "sh": mod.sh,
        "ensure_runtime_canonical_config": mod.ensure_runtime_canonical_config,
    }
    calls: list[tuple[list[str], Path]] = []

    try:
        mod._acquire_lock = lambda _lock_path: 1  # type: ignore[assignment]
        mod._release_lock = lambda _fd, _lock_path: None  # type: ignore[assignment]
        mod.sh = lambda cmd, cwd, capture=True: calls.append((cmd, cwd)) or SimpleNamespace(returncode=0)  # type: ignore[assignment]
        mod.run_scan_scheduler_cli = (  # type: ignore[assignment]
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
            else SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        mod.run_pipeline_script = lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")  # type: ignore[assignment]
        mod.send_openclaw_message = lambda **_kwargs: SimpleNamespace(returncode=0, stdout='{"messageId":"m1"}', stderr="")  # type: ignore[assignment]
        mod.trading_day_via_futu = lambda _cfg, market: (True, str(market))  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = lambda *_args, **_kwargs: {}  # type: ignore[assignment]

        sys.argv = [
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
        rc = mod.main()
        assert rc == 0
        assert any("sent" in " ".join(cmd) for cmd, _ in calls)
    finally:
        sys.argv = old["argv"]
        mod._acquire_lock = old["acquire_lock"]  # type: ignore[assignment]
        mod._release_lock = old["release_lock"]  # type: ignore[assignment]
        mod.run_scan_scheduler_cli = old["run_scan_scheduler_cli"]  # type: ignore[assignment]
        mod.run_pipeline_script = old["run_pipeline_script"]  # type: ignore[assignment]
        mod.send_openclaw_message = old["send_openclaw_message"]  # type: ignore[assignment]
        mod.trading_day_via_futu = old["trading_day_via_futu"]  # type: ignore[assignment]
        mod.sh = old["sh"]  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = old["ensure_runtime_canonical_config"]  # type: ignore[assignment]


def test_send_if_needed_scheduler_view_prefers_is_notify_window_open_field() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    cfg_path = _test_config_path()
    old = {
        "argv": sys.argv[:],
        "acquire_lock": mod._acquire_lock,
        "release_lock": mod._release_lock,
        "run_scan_scheduler_cli": mod.run_scan_scheduler_cli,
        "run_pipeline_script": mod.run_pipeline_script,
        "send_openclaw_message": mod.send_openclaw_message,
        "trading_day_via_futu": mod.trading_day_via_futu,
        "sh": mod.sh,
        "ensure_runtime_canonical_config": mod.ensure_runtime_canonical_config,
    }
    calls: list[tuple[list[str], Path]] = []
    scheduler_calls: list[dict] = []
    send_calls = {"n": 0}

    try:
        mod._acquire_lock = lambda _lock_path: 1  # type: ignore[assignment]
        mod._release_lock = lambda _fd, _lock_path: None  # type: ignore[assignment]
        mod.sh = lambda cmd, cwd, capture=True: calls.append((cmd, cwd)) or SimpleNamespace(returncode=0)  # type: ignore[assignment]

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

        mod.run_scan_scheduler_cli = _scheduler  # type: ignore[assignment]
        mod.run_pipeline_script = lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")  # type: ignore[assignment]
        mod.send_openclaw_message = _send  # type: ignore[assignment]
        mod.trading_day_via_futu = lambda _cfg, market: (True, str(market))  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = lambda *_args, **_kwargs: {}  # type: ignore[assignment]

        sys.argv = [
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
        rc = mod.main()
        assert rc == 0
        assert send_calls["n"] == 0
        assert any("should_notify=False" in " ".join(cmd) for cmd, _ in calls)
        assert not any(bool(item.get("mark_notified")) for item in scheduler_calls)
    finally:
        sys.argv = old["argv"]
        mod._acquire_lock = old["acquire_lock"]  # type: ignore[assignment]
        mod._release_lock = old["release_lock"]  # type: ignore[assignment]
        mod.run_scan_scheduler_cli = old["run_scan_scheduler_cli"]  # type: ignore[assignment]
        mod.run_pipeline_script = old["run_pipeline_script"]  # type: ignore[assignment]
        mod.send_openclaw_message = old["send_openclaw_message"]  # type: ignore[assignment]
        mod.trading_day_via_futu = old["trading_day_via_futu"]  # type: ignore[assignment]
        mod.sh = old["sh"]  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = old["ensure_runtime_canonical_config"]  # type: ignore[assignment]


def test_send_if_needed_uses_normalized_notify_message_id_from_nested_payload() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    cfg_path = _test_config_path()
    old = {
        "argv": sys.argv[:],
        "acquire_lock": mod._acquire_lock,
        "release_lock": mod._release_lock,
        "run_scan_scheduler_cli": mod.run_scan_scheduler_cli,
        "run_pipeline_script": mod.run_pipeline_script,
        "send_openclaw_message": mod.send_openclaw_message,
        "trading_day_via_futu": mod.trading_day_via_futu,
        "sh": mod.sh,
        "ensure_runtime_canonical_config": mod.ensure_runtime_canonical_config,
    }
    calls: list[tuple[list[str], Path]] = []

    try:
        mod._acquire_lock = lambda _lock_path: 1  # type: ignore[assignment]
        mod._release_lock = lambda _fd, _lock_path: None  # type: ignore[assignment]
        mod.sh = lambda cmd, cwd, capture=True: calls.append((cmd, cwd)) or SimpleNamespace(returncode=0)  # type: ignore[assignment]
        mod.run_scan_scheduler_cli = (  # type: ignore[assignment]
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
            )
        )
        mod.run_pipeline_script = lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")  # type: ignore[assignment]
        mod.send_openclaw_message = (  # type: ignore[assignment]
            lambda **_kwargs: SimpleNamespace(returncode=0, stdout='{"result":{"messageId":"nested-42"}}', stderr="")
        )
        mod.trading_day_via_futu = lambda _cfg, market: (True, str(market))  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = lambda *_args, **_kwargs: {}  # type: ignore[assignment]

        sys.argv = [
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
        rc = mod.main()
        assert rc == 0
        assert any("message_id=nested-42" in " ".join(cmd) for cmd, _ in calls)
    finally:
        sys.argv = old["argv"]
        mod._acquire_lock = old["acquire_lock"]  # type: ignore[assignment]
        mod._release_lock = old["release_lock"]  # type: ignore[assignment]
        mod.run_scan_scheduler_cli = old["run_scan_scheduler_cli"]  # type: ignore[assignment]
        mod.run_pipeline_script = old["run_pipeline_script"]  # type: ignore[assignment]
        mod.send_openclaw_message = old["send_openclaw_message"]  # type: ignore[assignment]
        mod.trading_day_via_futu = old["trading_day_via_futu"]  # type: ignore[assignment]
        mod.sh = old["sh"]  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = old["ensure_runtime_canonical_config"]  # type: ignore[assignment]


def test_send_if_needed_treats_returncode_zero_without_message_id_as_unconfirmed() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    cfg_path = _test_config_path()
    old = {
        "argv": sys.argv[:],
        "acquire_lock": mod._acquire_lock,
        "release_lock": mod._release_lock,
        "run_scan_scheduler_cli": mod.run_scan_scheduler_cli,
        "run_pipeline_script": mod.run_pipeline_script,
        "send_openclaw_message": mod.send_openclaw_message,
        "trading_day_via_futu": mod.trading_day_via_futu,
        "sh": mod.sh,
        "ensure_runtime_canonical_config": mod.ensure_runtime_canonical_config,
    }
    calls: list[tuple[list[str], Path]] = []
    mark_calls = {"n": 0}

    try:
        mod._acquire_lock = lambda _lock_path: 1  # type: ignore[assignment]
        mod._release_lock = lambda _fd, _lock_path: None  # type: ignore[assignment]
        mod.sh = lambda cmd, cwd, capture=True: calls.append((cmd, cwd)) or SimpleNamespace(returncode=0)  # type: ignore[assignment]

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

        mod.run_scan_scheduler_cli = _scheduler  # type: ignore[assignment]
        mod.run_pipeline_script = lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")  # type: ignore[assignment]
        mod.send_openclaw_message = lambda **_kwargs: SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")  # type: ignore[assignment]
        mod.trading_day_via_futu = lambda _cfg, market: (True, str(market))  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = lambda *_args, **_kwargs: {}  # type: ignore[assignment]

        sys.argv = [
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
        rc = mod.main()
        assert rc == 1
        assert mark_calls["n"] == 0
        assert any("SEND_UNCONFIRMED" in " ".join(cmd) for cmd, _ in calls)
        assert any("message_id is missing" in " ".join(cmd) for cmd, _ in calls)
    finally:
        sys.argv = old["argv"]
        mod._acquire_lock = old["acquire_lock"]  # type: ignore[assignment]
        mod._release_lock = old["release_lock"]  # type: ignore[assignment]
        mod.run_scan_scheduler_cli = old["run_scan_scheduler_cli"]  # type: ignore[assignment]
        mod.run_pipeline_script = old["run_pipeline_script"]  # type: ignore[assignment]
        mod.send_openclaw_message = old["send_openclaw_message"]  # type: ignore[assignment]
        mod.trading_day_via_futu = old["trading_day_via_futu"]  # type: ignore[assignment]
        mod.sh = old["sh"]  # type: ignore[assignment]
        mod.ensure_runtime_canonical_config = old["ensure_runtime_canonical_config"]  # type: ignore[assignment]


def test_send_if_needed_trading_day_guard_market_inference_delegates_to_domain() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    old = mod.domain_markets_for_trading_day_guard
    seen: dict[str, object] = {}
    try:
        def _fake(markets_to_run, cfg, market_config):
            seen["markets_to_run"] = list(markets_to_run or [])
            seen["cfg"] = cfg
            seen["market_config"] = market_config
            return ["HK"]

        mod.domain_markets_for_trading_day_guard = _fake  # type: ignore[assignment]
        cfg = {"symbols": [{"symbol": "0700.HK", "market": "HK"}]}
        out = mod._infer_trading_day_guard_markets(cfg)
        assert out == ["HK"]
        assert seen["markets_to_run"] == []
        assert seen["cfg"] is cfg
        assert seen["market_config"] == "auto"
    finally:
        mod.domain_markets_for_trading_day_guard = old  # type: ignore[assignment]


def main() -> None:
    test_send_if_needed_scheduler_view_compat_should_notify_field()
    test_send_if_needed_scheduler_view_prefers_is_notify_window_open_field()
    test_send_if_needed_uses_normalized_notify_message_id_from_nested_payload()
    test_send_if_needed_treats_returncode_zero_without_message_id_as_unconfirmed()
    test_send_if_needed_trading_day_guard_market_inference_delegates_to_domain()
    print("OK (send-if-needed-batch3)")


if __name__ == "__main__":
    main()
