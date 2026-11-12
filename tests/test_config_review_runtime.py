from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.reports.config_review import build_config_review
from src.application.reports.prices import load_runtime_scan_prices
from src.application.required_data_snapshot import retire_required_data_snapshot_shadows, seal_required_data_snapshot
from tests import test_required_data_snapshot as frozen_fixture
from tests.test_runtime_config_identity import _inline_runtime_config


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _runtime_config(root: Path, market: str, symbol: str, minimum: int = 100, *, as_list: bool = False) -> Path:
    payload = _inline_runtime_config(market=market.lower())
    settings = {"sell_call": {"enabled": True, "min_strike": minimum, "min_dte": 7, "max_dte": 60}}
    payload["symbols"] = [{"symbol": symbol, **settings}] if as_list else {symbol: settings}
    path = root / f"config.{market.lower()}.json"
    path.write_text(json.dumps(payload))
    return path


def _scan(root: Path, monkeypatch: pytest.MonkeyPatch, *, run_id: str, symbol: str, market: str,
          sealed_at: datetime, observed_at: datetime | None = None, successful: bool = True) -> Path:
    quote_time = (observed_at or sealed_at - timedelta(minutes=1)).isoformat()
    required_root, manifest_path = frozen_fixture._workspace(root, run_id)
    original_save = frozen_fixture.save_outputs

    def save_with_quote(base, symbol, payload, **kwargs):
        for row in payload["rows"]:
            row.update(spot_observed_at_utc=quote_time, spot_update_time=quote_time,
                       spot_age_seconds=0, underlier_observation_status="ready")
        return original_save(base, symbol, payload, **kwargs)

    original_publish = frozen_fixture.publish_required_data_quote_snapshot

    def publish_at_quote_time(**kwargs):
        return original_publish(**kwargs, now=datetime.fromisoformat(quote_time))

    with monkeypatch.context() as scoped:
        scoped.setattr(frozen_fixture, "_OBSERVED_AT", quote_time)
        scoped.setattr(frozen_fixture, "save_outputs", save_with_quote)
        scoped.setattr(frozen_fixture, "publish_required_data_quote_snapshot", publish_at_quote_time)
        frozen_fixture._publish_quote(required_root, run_id=run_id, symbol=symbol, canonical_blob=True)
        manifest = seal_required_data_snapshot(
            manifest_path=manifest_path, required_data_root=required_root, run_id=run_id,
            prefetch_summary=frozen_fixture._summary(symbol), sealed_at=sealed_at,
        )
    retire_required_data_snapshot_shadows(
        manifest_path=manifest_path, required_data_root=required_root,
        manifest=manifest, manifest_bytes=manifest_path.read_bytes(),
    )
    metrics = {"markets_to_run": [market], "accounts": [{"account": "fixture", "ran_scan": True}],
               "scan_outcomes": {"fixture": successful}}
    (manifest_path.parent / "tick_metrics.json").write_text(json.dumps(metrics))
    assert not (required_root / manifest["symbols"][symbol]["raw_json_relpath"]).exists()
    return manifest_path


@pytest.mark.parametrize("as_list", [False, True])
def test_report_defaults_to_generated_runtime_config_and_preserves_explicit_local_source(tmp_path: Path, as_list: bool) -> None:
    _runtime_config(tmp_path, "US", "PDD", 100, as_list=as_list)
    _runtime_config(tmp_path, "HK", "0700.HK", as_list=as_list)
    local = tmp_path / "configs"
    local.mkdir()
    (local / "user.us.json").write_text(json.dumps({"symbols": [{"symbol": "PDD", "sell_call": {"min_strike": 130}}]}))
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"generated_at_utc": NOW.isoformat(), "prices": {"PDD": 80}}))
    runtime = build_config_review(runtime_root=tmp_path, prices_file=prices, now=NOW)
    pdd = next(m for m in runtime.payload["markets"] if m["market"] == "US")["symbols"][0]
    assert pdd["strategies"]["sell_call"]["min_strike"] == 100
    assert runtime.payload["config_source"] == "runtime"
    assert "config.us.json" in runtime.markdown
    assert "user.us.json" not in runtime.markdown
    local_result = build_config_review(runtime_root=tmp_path, configs_root=local, prices_file=prices, now=NOW)
    assert local_result.payload["config_source"] == "local_user"
    assert local_result.payload["markets"][0]["symbols"][0]["strategies"]["sell_call"]["min_strike"] == 130


def test_cli_default_reads_runtime_without_local_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.interfaces.cli.main import main

    _runtime_config(tmp_path, "US", "PDD", 100, as_list=True)
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"generated_at_utc": NOW.isoformat(), "prices": {"PDD": 80}}))
    assert main(["report", "run", "config_review", "--runtime-root", str(tmp_path),
                 "--prices-file", str(prices), "--dry-run", "--no-deliver", "--json"]) == 0
    outcome = json.loads(capsys.readouterr().out)
    payload = json.loads(next(item["content"] for item in outcome["artifacts"] if item["name"] == "report.json"))
    assert payload["config_source"] == "runtime"
    assert payload["markets"][0]["symbols"][0]["strategies"]["sell_call"]["min_strike"] == 100
    assert not (tmp_path / "reports").exists()


def test_invalid_runtime_identity_does_not_fall_back_to_local_config(tmp_path: Path) -> None:
    path = _runtime_config(tmp_path, "US", "PDD")
    data = json.loads(path.read_text())
    data["_generated"]["market"] = "hk"
    path.write_text(json.dumps(data))
    with pytest.raises(AgentToolError, match=".*") as caught:
        build_config_review(runtime_root=tmp_path, now=NOW)
    assert caught.value.code == "CONFIG_ERROR"


def test_prices_use_shared_blobs_seal_order_and_real_quote_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    older = _scan(tmp_path, monkeypatch, run_id="z-older", symbol="PDD", market="US", sealed_at=NOW - timedelta(days=3))
    recent = _scan(tmp_path, monkeypatch, run_id="a-recent", symbol="PDD", market="US", sealed_at=NOW - timedelta(days=2))
    _scan(tmp_path, monkeypatch, run_id="hk", symbol="3690.HK", market="HK", sealed_at=NOW - timedelta(days=2))
    os.utime(older, (NOW.timestamp(), NOW.timestamp()))
    os.utime(recent, (1, 1))
    snapshot = load_runtime_scan_prices(tmp_path, now=NOW)
    assert snapshot.runs == {"HK": "hk", "US": "a-recent"}
    assert snapshot.get("PDD").price == 110
    assert snapshot.get("PDD").observed_at_utc == (NOW - timedelta(days=2, minutes=1)).isoformat()
    assert snapshot.get("3690.HK").price == 110
    assert snapshot.data_gaps == ()


@pytest.mark.parametrize("bad_kind", ["failed", "missing_success", "conflicting_account", "incomplete", "corrupt_blob", "future"])
def test_latest_unusable_scan_does_not_mask_previous_usable_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_kind: str) -> None:
    _scan(tmp_path, monkeypatch, run_id="old", symbol="PDD", market="US", sealed_at=NOW - timedelta(days=2))
    latest = _scan(tmp_path, monkeypatch, run_id="new", symbol="PDD", market="US", sealed_at=NOW - timedelta(days=1))
    metrics_path = latest.parent / "tick_metrics.json"
    metrics = json.loads(metrics_path.read_text())
    manifest = json.loads(latest.read_text())
    if bad_kind == "failed":
        metrics["scan_outcomes"]["fixture"] = False
    elif bad_kind == "missing_success":
        metrics.pop("scan_outcomes")
    elif bad_kind == "conflicting_account":
        metrics["scan_outcomes"] = {"other": True, "fixture": False}
    elif bad_kind == "incomplete":
        manifest["status"] = "partial"
    elif bad_kind == "corrupt_blob":
        (tmp_path / manifest["symbols"]["PDD"]["scan_blob_ref"]["blob_relpath"]).write_bytes(b"bad")
    else:
        manifest["sealed_at_utc"] = (NOW + timedelta(days=1)).isoformat()
    latest.write_text(json.dumps(manifest))
    metrics_path.write_text(json.dumps(metrics))
    snapshot = load_runtime_scan_prices(tmp_path, markets=["US"], now=NOW)
    assert snapshot.runs == {"US": "old"}
    assert snapshot.get("PDD").price == 110
    assert snapshot.data_gaps or snapshot.diagnostics


def test_stale_quote_stays_stale_and_missing_symbol_is_not_filled_from_older_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _runtime_config(tmp_path, "US", "PDD")
    config = json.loads(config_path.read_text())
    config["symbols"]["NVDA"] = dict(config["symbols"]["PDD"])
    config_path.write_text(json.dumps(config))
    _scan(tmp_path, monkeypatch, run_id="old-with-pdd", symbol="PDD", market="US", sealed_at=NOW - timedelta(days=6))
    _scan(tmp_path, monkeypatch, run_id="latest", symbol="NVDA", market="US", sealed_at=NOW - timedelta(days=5))
    result = build_config_review(runtime_root=tmp_path, now=NOW)
    assert result.payload["price_runs"] == {"US": "latest"}
    rows = {row["symbol"]: row for row in result.payload["markets"][0]["symbols"]}
    assert rows["PDD"]["price"] is None
    assert rows["NVDA"]["price"]["value"] == 110
    assert all(row["conclusion"] == "观察" for row in result.payload["boundary_assessments"])
    assert any("96" in row["reason"] for row in result.payload["boundary_assessments"] if row["symbol"] == "NVDA")
    snapshot = load_runtime_scan_prices(tmp_path, markets=["US"], now=NOW)
    assert snapshot.get("NVDA").observed_at_utc == (NOW - timedelta(days=5, minutes=1)).isoformat()
    assert snapshot.get("PDD") is None


def test_no_brief_fallback_when_no_scan_exists(tmp_path: Path) -> None:
    state = tmp_path / "output_accounts" / "fixture" / "state"
    state.mkdir(parents=True)
    (state / "daily_decision_brief.US.current.json").write_text(json.dumps({
        "generated_at_utc": NOW.isoformat(), "candidate_index": [{"representative": {"metrics": {"symbol": "PDD", "spot": 130}}}],
    }))
    snapshot = load_runtime_scan_prices(tmp_path, markets=["US"], now=NOW)
    assert snapshot.get("PDD") is None
    assert any("US" in gap for gap in snapshot.data_gaps)


@pytest.mark.parametrize("status,message_id,command_exit,expected", [
    ("delivered", "fixture-message", 0, 0), ("delivered", None, 0, 1),
    ("skipped", None, 0, 1), ("failed", None, 1, 1),
])
def test_systemd_runner_requires_delivery_without_duplicate_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, message_id: str | None, command_exit: int, expected: int) -> None:
    spec = importlib.util.spec_from_file_location("systemd_weekly_fixture", ROOT / "deploy/systemd/run-config-review-weekly.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, command_exit, json.dumps({
            "ok": True, "status": "succeeded", "delivery": {"status": status, "message_id": message_id},
        }), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main(["--source-root", str(ROOT), "--runtime-root", str(tmp_path)]) == expected
    assert len(calls) == 1
    assert "--configs-root" not in calls[0]
    assert "--prices-file" not in calls[0]
    assert calls[0][-1] == "--json"
