from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

import pytest

import src.application.trades.auto_intake as auto_intake

from src.application.layered_config import build_layered_runtime_config_from_user_config
from src.application.runtime_config_freshness import GENERATED_KEY, build_inline_generated_metadata


BASE = Path(__file__).resolve().parents[1]
AUTO_INTAKE_CLI_TIMEOUT_SEC = 15


def _listener_source(tmp_path: Path, account: str, port: int) -> dict:
    return {
        "id": account,
        "account": account,
        "enabled": True,
        "mode": "apply",
        "host": "127.0.0.1",
        "port": port,
        "state_path": Path(f"state/{account}.json"),
        "audit_path": Path(f"audit/{account}.jsonl"),
        "status_path": Path(f"status/{account}.json"),
        "inbox_path": Path(f"inbox/{account}.sqlite3"),
        "backfill_checkpoint_path": Path(f"backfill/{account}.json"),
        "reconnect_sec": 5,
        "receipt": {"enabled": True},
        "backfill": {"enabled": False},
        "account_mapping": {f"REAL_{account.upper()}": account},
        "futu_account_ids": [f"REAL_{account.upper()}"],
        "combo_reconciliation_mode": "off",
    }


def _write_runtime_config(tmp_path: Path) -> Path:
    user_path = BASE / "configs" / "examples" / "user.example.us.json"
    user_config = json.loads(user_path.read_text(encoding="utf-8"))
    cfg, _meta = build_layered_runtime_config_from_user_config(
        repo_root=BASE,
        market="us",
        user_config=user_config,
        user_config_ref=str(user_path),
    )
    cfg[GENERATED_KEY] = build_inline_generated_metadata(
        repo_root=BASE,
        market="us",
        system_config_path=BASE / "configs" / "system.json",
        user_config=user_config,
        user_config_ref=str(user_path),
    )
    path = tmp_path / "config.us.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_open_deal_payload(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "deal_id": "example-open-0700hk-20260429-480p-1",
                "order_id": "example-order-open-1",
                "trd_acc_id": "REAL_12345678",
                "code": "0700.HK",
                "option_type": "PUT",
                "side": "SELL",
                "position_effect": "OPEN",
                "qty": 2,
                "price": 3.93,
                "strike": 480,
                "multiplier": 100,
                "expiration": "20260429",
                "currency": "HKD",
                "create_time": "2026-04-09 13:10:25",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_auto_trade_intake_open_example_dry_run_without_explicit_data_config(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    deal_path = _write_open_deal_payload(tmp_path / "auto_trade_intake.open.json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--mode",
            "dry-run",
            "--deal-json",
            str(deal_path),
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
        timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["action"] == "open"


def test_auto_trade_intake_apply_mode_requires_confirm(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    deal_path = _write_open_deal_payload(tmp_path / "auto_trade_intake.open.json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--mode",
            "apply",
            "--deal-json",
            str(deal_path),
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
        timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
    )

    assert result.returncode == 2
    assert "use --confirm or --yes" in result.stdout


def test_auto_trade_intake_retry_failed_requires_deal_json(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--mode",
            "dry-run",
            "--retry-failed",
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
        timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
    )

    assert result.returncode == 2
    assert "--retry-failed requires --deal-json replay" in result.stdout


def test_auto_trade_intake_dry_run_flag_is_reconcile_state_only(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--dry-run",
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
        timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
    )

    assert result.returncode == 2
    assert (
        "--dry-run is only supported with --reconcile-state or "
        "--compensate-receipts"
    ) in result.stdout


def test_auto_trade_intake_once_defaults_state_paths_to_runtime_root(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    runtime_root = tmp_path / "runtime"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--mode",
            "dry-run",
            "--once",
        ],
        cwd=str(BASE),
        env={**dict(os.environ), "OM_RUNTIME_ROOT": str(runtime_root)},
        capture_output=True,
        text=True,
        check=False,
        timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["state_path"] == str((runtime_root / "output_shared" / "state" / "auto_trade_intake_state.json").resolve())
    assert payload["audit_path"] == str((runtime_root / "output_shared" / "state" / "auto_trade_intake_audit.jsonl").resolve())
    assert payload["status_path"] == str((runtime_root / "output_shared" / "state" / "auto_trade_intake_status.json").resolve())
    assert payload["runtime_root"] == str(runtime_root.resolve())
    assert payload["runtime_root_source"] == "env:OM_RUNTIME_ROOT"


def test_auto_trade_intake_once_accepts_explicit_runtime_root_over_env(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    explicit_runtime_root = tmp_path / "runtime-argument"
    env_runtime_root = tmp_path / "runtime-env"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--runtime-root",
            str(explicit_runtime_root),
            "--mode",
            "dry-run",
            "--once",
        ],
        cwd=str(BASE),
        env={**dict(os.environ), "OM_RUNTIME_ROOT": str(env_runtime_root)},
        capture_output=True,
        text=True,
        check=False,
        timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["runtime_root"] == str(explicit_runtime_root.resolve())
    assert payload["runtime_root_source"] == "argument"
    assert payload["state_path"] == str((explicit_runtime_root / "output_shared" / "state" / "auto_trade_intake_state.json").resolve())
    assert payload["audit_path"] == str((explicit_runtime_root / "output_shared" / "state" / "auto_trade_intake_audit.jsonl").resolve())
    assert payload["status_path"] == str((explicit_runtime_root / "output_shared" / "state" / "auto_trade_intake_status.json").resolve())


@pytest.mark.parametrize("source_count", (1, 2))
def test_listener_main_owns_exactly_one_lifecycle_batch_dispatcher(
    monkeypatch,
    tmp_path: Path,
    source_count: int,
) -> None:
    sources = [
        _listener_source(tmp_path, account, 11111 + index)
        for index, account in enumerate(("lx", "sy")[:source_count])
    ]
    intake_cfg = {
        "enabled": True,
        "mode": "apply",
        "state_path": Path("state.json"),
        "audit_path": Path("audit.jsonl"),
        "status_path": Path("status.json"),
        "receipt": {"enabled": True},
        "backfill": {"enabled": False},
        "holdings_sync": {"enabled": False},
        "combo_reconciliation": {
            "default_mode": "off",
            "accounts": {},
        },
        "account_mapping": {
            f"REAL_{account.upper()}": account
            for account in ("lx", "sy")[:source_count]
        },
        "futu_account_ids": [
            f"REAL_{account.upper()}"
            for account in ("lx", "sy")[:source_count]
        ],
        "sources": sources,
    }
    instances: list[object] = []

    class _Dispatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.start_count = 0
            self.close_count = 0
            instances.append(self)

        def start(self):
            self.start_count += 1

        def close(self):
            self.close_count += 1

        def snapshot(self):
            return {
                "schema_version": (
                    "trade_lifecycle_batch_dispatcher_status.v1"
                ),
                "status": (
                    "running" if self.start_count else "initialized"
                ),
            }

    observed_statuses: list[dict] = []

    def _run_source(**kwargs):
        observed_statuses.append(
            kwargs["lifecycle_dispatcher_status_fn"]()
        )
        return 0

    def _run_sources(sources, *, run_source):
        stop_event = threading.Event()
        return max(run_source(source, stop_event) for source in sources)

    monkeypatch.setattr(auto_intake, "load_config", lambda **_kwargs: {})
    monkeypatch.setattr(
        auto_intake,
        "resolve_trade_intake_config",
        lambda *_args, **_kwargs: intake_cfg,
    )
    monkeypatch.setattr(
        auto_intake,
        "open_position_ledger_from_runtime_config",
        lambda **_kwargs: (None, object()),
    )
    monkeypatch.setattr(
        auto_intake,
        "resolve_trade_lifecycle_notification_batch_route",
        lambda **_kwargs: {
            "provider": "feishu_app",
            "channel": "bot",
            "target": "secret-target",
            "target_fingerprint": "target-fingerprint",
            "route_fingerprint": "route-fingerprint",
            "route_available": True,
        },
    )
    monkeypatch.setattr(
        auto_intake,
        "LifecycleReceiptBatchDispatcher",
        _Dispatcher,
    )
    monkeypatch.setattr(
        auto_intake,
        "_run_listener_source_loop",
        _run_source,
    )
    monkeypatch.setattr(
        auto_intake,
        "_coordinate_listener_sources",
        _run_sources,
    )

    rc = auto_intake.main(
        [
            "--config",
            str(tmp_path / "config.us.json"),
            "--runtime-root",
            str(tmp_path),
            "--mode",
            "apply",
            "--confirm",
        ]
    )

    assert rc == 0
    assert len(instances) == 1
    dispatcher = instances[0]
    assert dispatcher.start_count == 1
    assert dispatcher.close_count == 1
    assert dispatcher.kwargs["allowed_accounts"] == [
        account for account in ("lx", "sy")[:source_count]
    ]
    assert len(observed_statuses) == source_count
    assert {item["status"] for item in observed_statuses} == {"running"}


@pytest.mark.parametrize(
    ("apply_changes", "receipt_enabled", "reason"),
    (
        (False, True, "dry_run"),
        (True, False, "receipt_disabled"),
    ),
)
def test_lifecycle_dispatcher_is_not_built_for_dry_run_or_disabled_receipts(
    monkeypatch,
    tmp_path: Path,
    apply_changes: bool,
    receipt_enabled: bool,
    reason: str,
) -> None:
    source = _listener_source(tmp_path, "lx", 11111)
    source["receipt"] = {"enabled": receipt_enabled}
    monkeypatch.setattr(
        auto_intake,
        "resolve_trade_lifecycle_notification_batch_route",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled dispatcher must not resolve a route")
        ),
    )

    dispatcher, status_fn = (
        auto_intake._build_lifecycle_receipt_batch_dispatcher(
            repo=object(),
            base=tmp_path,
            cfg={},
            intake_cfg={
                "receipt": {"enabled": receipt_enabled},
                "sources": [source],
            },
            apply_changes=apply_changes,
        )
    )

    assert dispatcher is None
    assert status_fn()["status"] == "disabled"
    assert status_fn()["reason"] == reason


def test_lifecycle_dispatcher_is_not_built_when_route_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = _listener_source(tmp_path, "lx", 11111)
    monkeypatch.setattr(
        auto_intake,
        "resolve_trade_lifecycle_notification_batch_route",
        lambda **_kwargs: {
            "provider": "feishu_app",
            "channel": "bot",
            "route_available": False,
        },
    )

    dispatcher, status_fn = (
        auto_intake._build_lifecycle_receipt_batch_dispatcher(
            repo=object(),
            base=tmp_path,
            cfg={},
            intake_cfg={
                "receipt": {"enabled": True},
                "sources": [source],
            },
            apply_changes=True,
        )
    )

    assert dispatcher is None
    assert status_fn()["status"] == "unavailable"
    assert status_fn()["reason"] == "route_unavailable"


def test_source_listener_has_no_lifecycle_provider_send_path() -> None:
    import inspect

    source = inspect.getsource(auto_intake._run_listener_source_loop)

    assert "send_trade_lifecycle_outbox_payload" not in source
    assert "dispatch_notification" not in source


def test_source_loop_retries_checkpoint_before_building_settlement_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []

    class Repo:
        def list_trade_lifecycle_attempt_audit_heads_for_account(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return []

    class Listener:
        def __init__(self, **_kwargs):
            return None

        def start(self, **_kwargs):
            return None

        def check_health(self):
            return None

        def close(self):
            return None

    class History:
        def __init__(self, **_kwargs):
            return None

        def fetch(self):
            return []

        def close(self):
            return None

    class Gateway:
        def close(self):
            return None

    class Stop:
        stopped = False
        waits = 0

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, _seconds):
            self.waits += 1
            if self.waits >= 2:
                self.stopped = True
            return self.stopped

    original_checkpoint = (
        auto_intake.append_lifecycle_attempt_checkpoint_seal
    )
    checkpoint_attempts = 0

    def checkpoint(*args, **kwargs):
        nonlocal checkpoint_attempts
        checkpoint_attempts += 1
        order.append(str(kwargs["reason"]))
        if checkpoint_attempts == 1:
            raise OSError("disk full")
        return original_checkpoint(*args, **kwargs)

    monotonic = 0

    def next_monotonic():
        nonlocal monotonic
        monotonic += 61
        return float(monotonic)

    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", Listener)
    monkeypatch.setattr(auto_intake, "OpenDHistoryDealClient", History)
    monkeypatch.setattr(
        auto_intake,
        "append_lifecycle_attempt_checkpoint_seal",
        checkpoint,
    )
    monkeypatch.setattr(
        auto_intake,
        "build_futu_gateway",
        lambda **_kwargs: order.append("gateway") or Gateway(),
    )
    monkeypatch.setattr(
        auto_intake,
        "resolve_futu_quote_route",
        lambda _cfg: SimpleNamespace(
            ok=False,
            errors=("quote unavailable",),
            status="unavailable",
            host=None,
            port=None,
        ),
    )
    monkeypatch.setattr(
        auto_intake,
        "reconcile_due_lifecycle_cases_for_source",
        lambda *_args, **kwargs: order.append("runtime")
        or {
            "seal_status": "not_required",
            "run_seal": None,
            "process_counters": kwargs["process_metrics"],
        },
    )
    monkeypatch.setattr(
        auto_intake,
        "trade_inbox_summary",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        auto_intake,
        "list_retryable_trade_payloads",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        auto_intake,
        "_refresh_lifecycle_delivery_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(auto_intake.time, "monotonic", next_monotonic)
    source = {
        "id": "lx",
        "account": "lx",
        "host": "127.0.0.1",
        "port": 11111,
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
        "status_path": tmp_path / "status.json",
        "inbox_path": tmp_path / "inbox.sqlite3",
        "backfill_checkpoint_path": tmp_path / "backfill.json",
        "account_mapping": {"1001": "lx"},
        "futu_account_ids": ["1001"],
        "backfill": {"enabled": False},
    }

    rc = auto_intake._run_listener_source_loop(
        source=source,
        repo=Repo(),
        cfg={},
        cfg_path=tmp_path / "config.json",
        runtime_root=tmp_path,
        runtime_root_source="test",
        intake_cfg={
            "mode": "apply",
            "enabled": True,
            "backfill": {"enabled": False},
        },
        apply_changes=True,
        receipt_callback=lambda _context: {},
        process_lock=threading.RLock(),
        stop_event=Stop(),
    )

    assert rc == 0
    assert order == [
        "process_startup",
        "prior_seal_persist_failed",
        "gateway",
        "runtime",
    ]


def test_reconcile_intake_sources_defaults_to_every_account(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[Path] = []

    def _reconcile(**kwargs):
        calls.append(Path(kwargs["state_path"]))
        return {
            "planned_count": 1,
            "applied_count": 0,
            "backup_path": None,
        }

    monkeypatch.setattr(auto_intake, "reconcile_trade_intake_state", _reconcile)
    sources = [
        {
            "id": "lx",
            "account": "lx",
            "state_path": tmp_path / "lx" / "state.json",
            "audit_path": tmp_path / "lx" / "audit.jsonl",
        },
        {
            "id": "sy",
            "account": "sy",
            "state_path": tmp_path / "sy" / "state.json",
            "audit_path": tmp_path / "sy" / "audit.jsonl",
        },
    ]

    out = auto_intake._reconcile_intake_sources(
        sources=sources,
        repo=object(),
        account=None,
        deal_ids=[],
        apply_changes=False,
        runtime_root=tmp_path,
        runtime_root_source="test",
    )

    assert calls == [
        tmp_path / "lx" / "state.json",
        tmp_path / "sy" / "state.json",
    ]
    assert out["source_count"] == 2
    assert out["planned_count"] == 2
    assert out["dry_run"] is True


def test_auto_trade_intake_once_reports_multiple_account_sources(tmp_path: Path) -> None:
    user_path = tmp_path / "user.us.json"
    user_path.write_text(
        json.dumps(
            {
                "account_settings": {
                    "lx": {
                        "type": "futu",
                        "futu": {"account_id": "REAL_12345678", "host": "127.0.0.1", "port": 11111},
                    },
                    "sy": {
                        "type": "futu",
                        "futu": {"account_id": "REAL_87654321", "host": "127.0.0.1", "port": 11112},
                    },
                },
                "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    user_config = json.loads(user_path.read_text(encoding="utf-8"))
    cfg, _meta = build_layered_runtime_config_from_user_config(
        repo_root=BASE,
        market="us",
        user_config=user_config,
        user_config_ref=str(user_path),
    )
    cfg[GENERATED_KEY] = build_inline_generated_metadata(
        repo_root=BASE,
        market="us",
        system_config_path=BASE / "configs" / "system.json",
        user_config=user_config,
        user_config_ref=str(user_path),
    )
    config_path = tmp_path / "config.us.json"
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runtime_root = tmp_path / "runtime"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.application.trades.auto_intake",
            "--config",
            str(config_path),
            "--runtime-root",
            str(runtime_root),
            "--once",
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
        timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert [item["id"] for item in payload["sources"]] == ["lx", "sy"]
    assert [item["port"] for item in payload["sources"]] == [11111, 11112]
    assert payload["sources"][0]["state_path"] == str((runtime_root / "output_shared/state/trade_intake/lx/state.json").resolve())
    assert payload["sources"][1]["status_path"] == str((runtime_root / "output_shared/state/trade_intake/sy/status.json").resolve())


def test_deal_json_source_selection_uses_payload_futu_account_id() -> None:
    from src.application.trades.auto_intake import _select_source_for_payload

    sources = [
        {
            "id": "lx",
            "account": "lx",
            "host": "127.0.0.1",
            "port": 11111,
            "account_mapping": {"REAL_12345678": "lx"},
            "futu_account_ids": ["REAL_12345678"],
        },
        {
            "id": "sy",
            "account": "sy",
            "host": "127.0.0.1",
            "port": 11112,
            "account_mapping": {"REAL_87654321": "sy"},
            "futu_account_ids": ["REAL_87654321"],
        },
    ]

    selected = _select_source_for_payload(
        sources,
        payload={"deal_id": "deal-sy-1", "futu_account_id": "REAL_87654321"},
        account_mapping={"REAL_12345678": "lx", "REAL_87654321": "sy"},
        require_match=True,
    )

    assert selected["id"] == "sy"
    assert selected["port"] == 11112


def test_deal_json_apply_requires_source_match_when_multiple_sources() -> None:
    import pytest

    from src.application.trades.auto_intake import _select_source_for_payload

    sources = [
        {"id": "lx", "account": "lx", "futu_account_ids": ["REAL_12345678"]},
        {"id": "sy", "account": "sy", "futu_account_ids": ["REAL_87654321"]},
    ]

    with pytest.raises(SystemExit, match="requires payload futu_account_id/account"):
        _select_source_for_payload(
            sources,
            payload={"deal_id": "deal-no-account"},
            account_mapping={"REAL_12345678": "lx", "REAL_87654321": "sy"},
            require_match=True,
        )


def test_deal_json_source_selection_rejects_account_mapping_conflict() -> None:
    import pytest

    from src.application.trades.auto_intake import _select_source_for_payload

    sources = [
        {"id": "lx", "account": "lx", "futu_account_ids": ["REAL_12345678"]},
        {"id": "sy", "account": "sy", "futu_account_ids": ["REAL_87654321"]},
    ]

    with pytest.raises(SystemExit, match="account conflicts"):
        _select_source_for_payload(
            sources,
            payload={"deal_id": "deal-conflict", "account": "lx", "futu_account_id": "REAL_87654321"},
            account_mapping={"REAL_12345678": "lx", "REAL_87654321": "sy"},
            require_match=True,
        )


def test_push_source_binding_supplies_account_identity_before_inbox() -> None:
    source = {
        "id": "sy",
        "account": "sy",
        "host": "127.0.0.1",
        "port": 11112,
        "account_mapping": {"REAL_87654321": "sy"},
        "futu_account_ids": ["REAL_87654321"],
    }
    push_payload = {
        "deal_id": "deal-expiry-1",
        "code": "HK.TCH260730P440000",
        "price": 0.0,
        "qty": 1.0,
        "trd_side": "BUY_BACK",
    }

    bound = auto_intake._bind_push_payload_to_source(
        push_payload,
        source=source,
        received_at_utc="2026-07-30T11:58:17+00:00",
    )

    assert bound["futu_account_id"] == "REAL_87654321"
    assert bound["_trade_intake_source"] == {
        "schema_version": "trade_intake_source.v1",
        "transport": "push",
        "source_id": "sy",
        "account": "sy",
        "futu_account_id": "REAL_87654321",
        "opend_process": "FutuOpenD",
        "opend_host": "127.0.0.1",
        "opend_port": 11112,
        "received_at_utc": "2026-07-30T11:58:17+00:00",
    }


def test_push_and_backfill_build_same_inbox_identity_regardless_of_arrival_order(
    tmp_path: Path,
) -> None:
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    from src.application.trades.inbox import enqueue_trade_payload, trade_inbox_summary

    source = {
        "id": "sy",
        "account": "sy",
        "host": "127.0.0.1",
        "port": 11112,
        "account_mapping": {"REAL_87654321": "sy"},
        "futu_account_ids": ["REAL_87654321"],
    }
    push_payload = auto_intake._bind_push_payload_to_source(
        {"deal_id": "same-deal", "code": "HK.TCH260730P440000"},
        source=source,
        received_at_utc="2026-07-30T11:58:17+00:00",
    )
    backfill_payload = {
        "deal_id": "same-deal",
        "code": "HK.TCH260730P440000",
        "futu_account_id": "REAL_87654321",
        "trd_acc_id": "REAL_87654321",
    }
    inputs = {
        "push": push_payload,
        "backfill": backfill_payload,
    }
    for first, second in (("push", "backfill"), ("backfill", "push")):
        path = tmp_path / f"{first}-first.sqlite3"
        ids = [
            enqueue_trade_payload(
                path,
                payload=inputs[source_name],
                source=source_name,
                broker_deal_key=broker_deal_key_from_payload(
                    inputs[source_name],
                    account_mapping=source["account_mapping"],
                ),
            )
            for source_name in (first, second)
        ]

        assert ids[0] == ids[1]
        assert trade_inbox_summary(path)["pending_count"] == 1


def test_push_source_binding_rejects_payload_from_another_account() -> None:
    import pytest

    with pytest.raises(ValueError, match="conflicts with OpenD source binding"):
        auto_intake._bind_push_payload_to_source(
            {
                "deal_id": "wrong-account",
                "futu_account_id": "REAL_87654321",
            },
            source={
                "id": "lx",
                "account": "lx",
                "host": "127.0.0.1",
                "port": 11111,
                "account_mapping": {"REAL_12345678": "lx"},
                "futu_account_ids": ["REAL_12345678"],
            },
            received_at_utc="2026-07-30T11:58:17+00:00",
        )


def test_push_source_binding_rejects_ambiguous_source_without_payload_account() -> None:
    import pytest

    with pytest.raises(ValueError, match="requires exactly one futu_account_id"):
        auto_intake._bind_push_payload_to_source(
            {"deal_id": "ambiguous"},
            source={
                "id": "shared",
                "host": "127.0.0.1",
                "port": 11111,
                "account_mapping": {
                    "REAL_12345678": "lx",
                    "REAL_87654321": "sy",
                },
                "futu_account_ids": ["REAL_12345678", "REAL_87654321"],
            },
            received_at_utc="2026-07-30T11:58:17+00:00",
        )


def test_listener_binds_push_source_before_enqueue(monkeypatch, tmp_path: Path) -> None:
    import threading

    captured: dict = {}

    class _Listener:
        def __init__(self, *, on_deal, **_kwargs):
            self.on_deal = on_deal

        def start(self, *, cancel_event):
            self.on_deal(
                {
                    "deal_id": "push-deal-1",
                    "code": "HK.TCH260730P440000",
                }
            )
            cancel_event.set()

        def check_health(self):
            raise AssertionError("listener health should not run after test push stops the source")

        def close(self):
            return None

    monkeypatch.setattr(auto_intake, "OpenDTradePushListener", _Listener)
    monkeypatch.setattr(
        auto_intake,
        "enqueue_trade_payload",
        lambda _path, *, payload, source, broker_deal_key: captured.update(
            payload=dict(payload),
            source=source,
            broker_deal_key=broker_deal_key,
        )
        or "inbox-1",
    )
    monkeypatch.setattr(
        auto_intake,
        "_process_payload",
        lambda payload, **_kwargs: {
            "status": "unresolved",
            "action": "lifecycle",
            "reason": "waiting_settlement_evidence",
            "deal_id": payload["deal_id"],
            "account": "sy",
            "diagnostics": {"retryable": True},
        },
    )
    monkeypatch.setattr(auto_intake, "settle_trade_payload_result", lambda *_args, **_kwargs: None)
    stop = threading.Event()
    source = {
        "id": "sy",
        "account": "sy",
        "host": "127.0.0.1",
        "port": 11112,
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
        "status_path": tmp_path / "status.json",
        "inbox_path": tmp_path / "inbox.sqlite3",
        "backfill_checkpoint_path": tmp_path / "backfill.json",
        "reconnect_sec": 5,
        "account_mapping": {"REAL_87654321": "sy"},
        "futu_account_ids": ["REAL_87654321"],
        "backfill": {"enabled": False},
    }

    rc = auto_intake._run_listener_source_loop(
        source=source,
        repo=object(),
        cfg={},
        cfg_path=tmp_path / "config.json",
        runtime_root=tmp_path,
        runtime_root_source="test",
        intake_cfg={
            "mode": "dry-run",
            "enabled": True,
            "account_mapping": source["account_mapping"],
            "backfill": {"enabled": False},
        },
        apply_changes=False,
        receipt_callback=lambda _context: {},
        process_lock=threading.RLock(),
        stop_event=stop,
    )

    assert rc == 0
    assert captured["source"] == "push"
    assert captured["payload"]["futu_account_id"] == "REAL_87654321"
    assert captured["payload"]["_trade_intake_source"]["source_id"] == "sy"
    assert captured["payload"]["_trade_intake_source"]["opend_port"] == 11112
    assert (
        captured["broker_deal_key"]
        == "futu:sy:REAL_87654321:push-deal-1"
    )


def test_auto_trade_intake_open_dry_run_accepts_futu_option_code_with_lookup_fields(tmp_path: Path) -> None:
    config_path = _write_runtime_config(tmp_path)
    payload_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            payload_path = f.name
            json.dump(
                {
                    "deal_id": "example-open-pop-20260528-150p-1",
                    "order_id": "example-order-open-pop-1",
                    "futu_account_id": "REAL_12345678",
                    "code": "HK.POP260528P150000",
                    "stock_name": "泡泡玛特",
                    "trd_side": "SELL_SHORT",
                    "qty": 1,
                    "price": 6.3,
                    "multiplier": 1000,
                    "create_time": "2026-04-28 10:15:56",
                },
                f,
                ensure_ascii=False,
            )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.application.trades.auto_intake",
                "--config",
                str(config_path),
                "--mode",
                "dry-run",
                "--deal-json",
                payload_path,
            ],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            check=False,
            timeout=AUTO_INTAKE_CLI_TIMEOUT_SEC,
        )
    finally:
        if payload_path:
            Path(payload_path).unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["action"] == "open"
    assert payload["account"] == "user1"
