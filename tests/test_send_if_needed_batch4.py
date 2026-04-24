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


def _patch_common(mod):
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
    mod._acquire_lock = lambda _lock_path: 1  # type: ignore[assignment]
    mod._release_lock = lambda _fd, _lock_path: None  # type: ignore[assignment]
    mod.sh = lambda _cmd, cwd=None, capture=True: SimpleNamespace(returncode=0)  # type: ignore[assignment]
    mod.trading_day_via_futu = lambda _cfg, market: (True, str(market))  # type: ignore[assignment]
    mod.run_pipeline_script = lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")  # type: ignore[assignment]
    mod.send_openclaw_message = lambda **_kwargs: SimpleNamespace(returncode=0, stdout='{"messageId":"m1"}', stderr="")  # type: ignore[assignment]
    mod.ensure_runtime_canonical_config = lambda *_args, **_kwargs: {}  # type: ignore[assignment]
    return old


def _restore(mod, old) -> None:
    sys.argv = old["argv"]
    mod._acquire_lock = old["acquire_lock"]  # type: ignore[assignment]
    mod._release_lock = old["release_lock"]  # type: ignore[assignment]
    mod.run_scan_scheduler_cli = old["run_scan_scheduler_cli"]  # type: ignore[assignment]
    mod.run_pipeline_script = old["run_pipeline_script"]  # type: ignore[assignment]
    mod.send_openclaw_message = old["send_openclaw_message"]  # type: ignore[assignment]
    mod.trading_day_via_futu = old["trading_day_via_futu"]  # type: ignore[assignment]
    mod.sh = old["sh"]  # type: ignore[assignment]
    mod.ensure_runtime_canonical_config = old["ensure_runtime_canonical_config"]  # type: ignore[assignment]


def test_send_if_needed_blocks_on_scheduler_snapshot_schema_failure() -> None:
    mod = importlib.import_module("scripts.send_if_needed")
    old = _patch_common(mod)
    cfg_path = _test_config_path()
    try:
        mod.run_scan_scheduler_cli = lambda **_kwargs: SimpleNamespace(returncode=0, stdout='["bad"]', stderr="")  # type: ignore[assignment]
        sys.argv = [
            "send_if_needed.py",
            "--config",
            str(cfg_path),
            "--state-dir",
            "output/state_test_send_if_needed_batch4",
            "--target",
            "user:test",
            "--notification",
            "README.md",
        ]
        try:
            mod.main()
            assert False, "expected SystemExit"
        except SystemExit as exc:
            msg = str(exc)
            assert "SCHEMA_VALIDATION_FAILED" in msg
            assert "scheduler_decision" in msg
    finally:
        _restore(mod, old)


def test_send_if_needed_blocks_on_decision_schema_failure() -> None:
    mod = importlib.import_module("scripts.send_if_needed")
    old = _patch_common(mod)
    cfg_path = _test_config_path()
    old_decision_from_payload = mod.Decision.from_payload
    try:
        mod.run_scan_scheduler_cli = lambda **_kwargs: SimpleNamespace(  # type: ignore[assignment]
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": True, "reason": "ok"}),
            stderr="",
        )
        mod.Decision.from_payload = classmethod(  # type: ignore[method-assign]
            lambda _cls, _raw: (_ for _ in ()).throw(mod.SchemaValidationError("bad decision"))
        )
        sys.argv = [
            "send_if_needed.py",
            "--config",
            str(cfg_path),
            "--state-dir",
            "output/state_test_send_if_needed_batch4",
            "--target",
            "user:test",
            "--notification",
            "README.md",
        ]
        try:
            mod.main()
            assert False, "expected SystemExit"
        except SystemExit as exc:
            msg = str(exc)
            assert "SCHEMA_VALIDATION_FAILED" in msg
            assert "scheduler_decision" in msg
    finally:
        mod.Decision.from_payload = old_decision_from_payload  # type: ignore[method-assign]
        _restore(mod, old)


def test_send_if_needed_skips_delivery_plan_when_not_sending() -> None:
    mod = importlib.import_module("scripts.send_if_needed")
    old = _patch_common(mod)
    cfg_path = _test_config_path()
    old_delivery_from_payload = mod.DeliveryPlan.from_payload
    try:
        mod.run_scan_scheduler_cli = lambda **_kwargs: SimpleNamespace(  # type: ignore[assignment]
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": False, "reason": "window closed"}),
            stderr="",
        )
        seen = {"called": False}
        mod.DeliveryPlan.from_payload = classmethod(  # type: ignore[method-assign]
            lambda _cls, _raw: seen.__setitem__("called", True)
        )
        sys.argv = [
            "send_if_needed.py",
            "--config",
            str(cfg_path),
            "--state-dir",
            "output/state_test_send_if_needed_batch4",
            "--target",
            "user:test",
            "--notification",
            "README.md",
        ]
        assert mod.main() == 0
        assert seen["called"] is False
    finally:
        mod.DeliveryPlan.from_payload = old_delivery_from_payload  # type: ignore[method-assign]
        _restore(mod, old)


def test_send_if_needed_blocks_on_pipeline_subprocess_adapter_schema_failure() -> None:
    mod = importlib.import_module("scripts.send_if_needed")
    old = _patch_common(mod)
    cfg_path = _test_config_path()
    old_normalize = mod.normalize_pipeline_subprocess_output
    try:
        mod.run_scan_scheduler_cli = lambda **_kwargs: SimpleNamespace(  # type: ignore[assignment]
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": False, "reason": "ok"}),
            stderr="",
        )
        mod.normalize_pipeline_subprocess_output = lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad pipeline adapter"))  # type: ignore[assignment]
        sys.argv = [
            "send_if_needed.py",
            "--config",
            str(cfg_path),
            "--state-dir",
            "output/state_test_send_if_needed_batch4",
            "--target",
            "user:test",
            "--notification",
            "README.md",
        ]
        try:
            mod.main()
            assert False, "expected SystemExit"
        except SystemExit as exc:
            msg = str(exc)
            assert "SCHEMA_VALIDATION_FAILED" in msg
            assert "pipeline_subprocess_adapter" in msg
    finally:
        mod.normalize_pipeline_subprocess_output = old_normalize  # type: ignore[assignment]
        _restore(mod, old)


def test_send_if_needed_blocks_on_notify_subprocess_adapter_schema_failure() -> None:
    mod = importlib.import_module("scripts.send_if_needed")
    old = _patch_common(mod)
    cfg_path = _test_config_path()
    old_normalize = mod.normalize_notify_subprocess_output
    try:
        mod.run_scan_scheduler_cli = lambda **_kwargs: SimpleNamespace(  # type: ignore[assignment]
            returncode=0,
            stdout=json.dumps({"should_run_scan": True, "is_notify_window_open": True, "reason": "ok"}),
            stderr="",
        )
        mod.normalize_notify_subprocess_output = lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad notify adapter"))  # type: ignore[assignment]
        sys.argv = [
            "send_if_needed.py",
            "--config",
            str(cfg_path),
            "--state-dir",
            "output/state_test_send_if_needed_batch4",
            "--target",
            "user:test",
            "--notification",
            "README.md",
        ]
        try:
            mod.main()
            assert False, "expected SystemExit"
        except SystemExit as exc:
            msg = str(exc)
            assert "SCHEMA_VALIDATION_FAILED" in msg
            assert "notify_subprocess_adapter" in msg
    finally:
        mod.normalize_notify_subprocess_output = old_normalize  # type: ignore[assignment]
        _restore(mod, old)
