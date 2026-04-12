from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_send_if_needed_scheduler_view_compat_should_notify_field() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    cfg_path = (BASE / "config.us.json").resolve()
    old = {
        "argv": sys.argv[:],
        "acquire_lock": mod._acquire_lock,
        "release_lock": mod._release_lock,
        "run_scan_scheduler_cli": mod.run_scan_scheduler_cli,
        "run_pipeline_script": mod.run_pipeline_script,
        "send_openclaw_message": mod.send_openclaw_message,
        "trading_day_via_futu": mod.trading_day_via_futu,
        "sh": mod.sh,
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


def test_send_if_needed_scheduler_view_prefers_is_notify_window_open_field() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    cfg_path = (BASE / "config.us.json").resolve()
    old = {
        "argv": sys.argv[:],
        "acquire_lock": mod._acquire_lock,
        "release_lock": mod._release_lock,
        "run_scan_scheduler_cli": mod.run_scan_scheduler_cli,
        "run_pipeline_script": mod.run_pipeline_script,
        "send_openclaw_message": mod.send_openclaw_message,
        "trading_day_via_futu": mod.trading_day_via_futu,
        "sh": mod.sh,
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


def test_send_if_needed_uses_normalized_notify_message_id_from_nested_payload() -> None:
    mod = importlib.import_module("scripts.send_if_needed")

    cfg_path = (BASE / "config.us.json").resolve()
    old = {
        "argv": sys.argv[:],
        "acquire_lock": mod._acquire_lock,
        "release_lock": mod._release_lock,
        "run_scan_scheduler_cli": mod.run_scan_scheduler_cli,
        "run_pipeline_script": mod.run_pipeline_script,
        "send_openclaw_message": mod.send_openclaw_message,
        "trading_day_via_futu": mod.trading_day_via_futu,
        "sh": mod.sh,
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


def main() -> None:
    test_send_if_needed_scheduler_view_compat_should_notify_field()
    test_send_if_needed_scheduler_view_prefers_is_notify_window_open_field()
    test_send_if_needed_uses_normalized_notify_message_id_from_nested_payload()
    print("OK (send-if-needed-batch3)")


if __name__ == "__main__":
    main()
