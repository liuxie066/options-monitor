from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.portfolio_scope import portfolio_scope_id
from src.application import pipeline_context as pc
from src.application.ledger import api
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.store_resolution import resolve_ledger_sqlite_path, resolve_ledger_store
from src.application.positions.context_cache import invalidate_option_positions_context_cache
from src.application.prepared_option_positions_context import PreparedOptionPositionsContextError


RATES = {"rates": {"USDCNY": 7.0, "HKDCNY": 0.9}}


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    return path


def _event(n: int, *, account="alpha", symbol="NVDA", kind="call", contracts=1) -> TradeEvent:
    return TradeEvent(
        event_id=f"synthetic-{n}", event_type="open", event_time_ms=1792500000000 + n,
        contract_key=ContractKey.from_values(
            broker="futu", account=account, underlying_symbol=symbol,
            option_type=kind, strike=90 + n, expiration_ymd="2027-06-18",
        ),
        contracts=contracts, price=2, currency="HKD" if symbol.endswith(".HK") else "USD",
        source="test", multiplier=100, lot_id=f"synthetic-lot-{n}", raw_payload={"side": "sell"},
    )


class _LedgerContext:
    """Real canonical ledger, snapshot, context and cache inputs; no broker I/O."""

    def __init__(self, root, monkeypatch):
        self.root = root
        self.config = _write(root / "synthetic-data.json", {})
        env = root / "empty.env"
        env.write_text("", encoding="utf-8")
        monkeypatch.setenv("OM_RUNTIME_ROOT", str(root))
        monkeypatch.setenv("OM_ENV_FILE", str(env))
        monkeypatch.setenv("OM_SECRET_BACKEND", "env")
        db = resolve_ledger_sqlite_path(self.config, runtime_root=root)
        assert db == root / "output_shared/state/option_positions.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        self.repo = SQLiteOptionPositionsRepository(db)
        assert resolve_ledger_store(self.config).sqlite_path == db
        self.logs = []
        self.observations = []
        monkeypatch.setattr(
            pc.state_repo, "append_source_snapshot_event",
            lambda _base, payload, **_kw: self.observations.append(copy.deepcopy(payload)),
        )
        self.args = dict(
            base=root, data_config=str(self.config), market="futu", account="alpha", ttl_sec=3600,
            state_dir=root / "output_accounts/alpha/state", shared_state_dir=root / "output_shared/state",
            log=self.logs.append, exchange_rate_observation=RATES,
            runtime_config={"accounts": ["alpha", "beta"]},
        )
        run_position_projection_forced_full(self.repo, [])

    def snapshot(self, account="alpha"):
        return api.decision_state_snapshot(self.repo, account=account, portfolio_scope_id=portfolio_scope_id(account))

    def context(self, account="alpha"):
        return api.build_position_risk_context(
            list(api.list_position_lot_snapshots(self.repo)), broker="futu", account=account,
            rates=RATES, decision_snapshot=self.snapshot(account),
        )

    def shared(self):
        return api.build_shared_position_risk_context(
            list(api.list_position_lot_snapshots(self.repo)), broker="futu", rates=RATES,
            decision_snapshots_by_account={a: self.snapshot(a) for a in ("alpha", "beta")},
        )

    def advance_a(self):
        run_position_projection_forced_full(self.repo, [
            _event(1), _event(2, symbol="0700.HK"), _event(3, account="beta", symbol="MSFT", contracts=3),
        ])

    def advance_b(self):
        run_position_projection_forced_full(self.repo, [_event(4), _event(5, kind="put")])

    def seed(self, source, context=None):
        if source == "account":
            return _write(self.args["state_dir"] / "option_positions_context.json",
                          self.context() if context is None else context)
        return _write(self.args["shared_state_dir"] / "option_positions_context.shared.json",
                      self.shared() if context is None else context)

    def load(self, **overrides):
        return pc.load_option_positions_context(**{**self.args, **overrides})

    def outer_args(self, monkeypatch):
        # These unrelated portfolio/FX boundaries cannot contact a provider.
        monkeypatch.setattr(pc, "load_portfolio_context", lambda **_kw: None)
        monkeypatch.setattr(pc, "get_exchange_rates_or_fetch_latest", lambda **_kw: RATES)
        monkeypatch.setattr(pc, "load_exchange_rates", lambda **_kw: (None, None))
        cfg = {"accounts": ["alpha", "beta"], "portfolio": {
            "account": "alpha", "broker": "futu", "data_config": str(self.config),
        }}
        return dict(
            py=sys.executable, base=self.root, cfg=cfg, report_dir=self.root / "report",
            portfolio_timeout_sec=1, runtime={}, is_scheduled=False,
            state_dir=self.args["state_dir"], shared_state_dir=self.args["shared_state_dir"],
            log=self.logs.append, no_context=False, want_scan=True,
        )


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    return _LedgerContext(tmp_path, monkeypatch)


def _assert_b(context, expected):
    assert context["decision_snapshot_status"] == "trusted"
    assert context["decision_state_fingerprint"] == expected["decision_state_fingerprint"]
    assert context["locked_shares_by_symbol"] == {"NVDA": 200.0, "0700.HK": 100.0}
    assert context["cash_secured_total_by_ccy"] == {"USD": 9500.0}
    assert context["filters"]["account"] == "alpha"
    assert "MSFT" not in context["locked_shares_by_symbol"]


@pytest.mark.parametrize("source", ["account", "shared"])
def test_trusted_old_fingerprint_refreshes_current_capacity(ledger, source):
    ledger.advance_a()
    old = ledger.context()
    ledger.seed(source)
    ledger.advance_b()
    current = ledger.context()
    assert old["decision_snapshot_status"] == current["decision_snapshot_status"] == "trusted"
    assert old["decision_state_fingerprint"] != current["decision_state_fingerprint"]
    out, refreshed = ledger.load()
    assert refreshed is True
    assert out["context_source"] == "shared_refresh"
    _assert_b(out, current)
    assert ledger.observations[-1]["source_name"] == "option_positions"


@pytest.mark.parametrize("source", ["account", "shared"])
@pytest.mark.parametrize("value", [None, "", "different", "padded"])
def test_cache_fingerprint_must_be_present_and_equal(ledger, source, value):
    ledger.advance_a()
    ledger.advance_b()
    payload = ledger.context() if source == "account" else ledger.shared()
    cached = payload if source == "account" else payload["by_account"]["alpha"]
    if value is None:
        cached.pop("decision_state_fingerprint")
    else:
        cached["decision_state_fingerprint"] = (
            f" {cached['decision_state_fingerprint']} " if value == "padded" else value
        )
    ledger.seed(source, payload)
    out, refreshed = ledger.load()
    assert refreshed is True and out["context_source"] == "shared_refresh"
    _assert_b(out, ledger.context())
    assert any("fingerprint" in line for line in ledger.logs)


@pytest.mark.parametrize("source", ["account", "shared"])
@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("cache_missing", [False, True])
def test_missing_current_fingerprint_cannot_qualify_cache(ledger, monkeypatch, source, value, cache_missing):
    ledger.advance_a()
    payload = ledger.context() if source == "account" else ledger.shared()
    cached = payload if source == "account" else payload["by_account"]["alpha"]
    if cache_missing:
        cached.pop("decision_state_fingerprint")
    ledger.seed(source, payload)
    original = pc.decision_state_snapshot
    calls = []

    def incomplete_first_observation(*args, **kwargs):
        snap = original(*args, **kwargs)
        calls.append(kwargs)
        if len(calls) == 1:
            # Narrow boundary injection: real trusted snapshot, absent field.
            if value is None:
                snap.pop("decision_state_fingerprint")
            else:
                snap["decision_state_fingerprint"] = value
        return snap

    monkeypatch.setattr(pc, "decision_state_snapshot", incomplete_first_observation)
    out, refreshed = ledger.load()
    assert refreshed is True and out["context_source"] == "shared_refresh"
    assert out["decision_state_fingerprint"] == ledger.snapshot()["decision_state_fingerprint"]
    assert len(calls) > 1


@pytest.mark.parametrize("source", ["account", "shared"])
@pytest.mark.parametrize("empty", [False, True])
def test_equal_fingerprint_keeps_hit_and_refreshed_semantics(ledger, monkeypatch, source, empty):
    if not empty:
        ledger.advance_a()
    expected = ledger.context()
    ledger.seed(source)
    monkeypatch.setattr(pc, "_load_option_position_records", lambda *_a: pytest.fail("equal cache must avoid refresh"))
    # Account-cache validation normalizes labels; shared lookup uses its exact key.
    out, refreshed = ledger.load(account="ALPHA" if source == "account" else "alpha", market="富途证券")
    assert refreshed is (source == "shared")
    assert out["context_source"] == ("account_cache" if source == "account" else "shared_slice")
    assert out["decision_state_fingerprint"] == expected["decision_state_fingerprint"]
    assert out["locked_shares_by_symbol"] == expected["locked_shares_by_symbol"]
    assert out["decision_snapshot_status"] == "trusted"
    assert len(ledger.observations) == 1


@pytest.mark.parametrize("source", ["account", "shared"])
@pytest.mark.parametrize("policy", ["zero", "expired"])
def test_equal_fingerprint_does_not_override_ttl(ledger, source, policy):
    ledger.advance_a()
    path = ledger.seed(source)
    if policy == "expired":
        os.utime(path, (time.time() - 7200, time.time() - 7200))
    out, refreshed = ledger.load(ttl_sec=0 if policy == "zero" else 3600)
    assert refreshed is True and out["context_source"] == "shared_refresh"


@pytest.mark.parametrize("source", ["account", "shared"])
@pytest.mark.parametrize("invalid", ["account", "broker", "family", "untrusted"])
def test_fingerprint_does_not_replace_existing_cache_contract(ledger, source, invalid):
    ledger.advance_a()
    context = ledger.context()
    if invalid == "account":
        context = ledger.context("beta")
    elif invalid == "broker":
        context["filters"]["broker"] = "synthetic-other-broker"
    elif invalid == "family":
        context["strategy_family_source"] = "legacy"
    else:
        context["decision_snapshot_status"] = "snapshot_unavailable"
    payload = context
    if source == "shared":
        payload = ledger.shared()
        payload["by_account"]["alpha"] = context
    ledger.seed(source, payload)
    out, refreshed = ledger.load()
    assert refreshed is True and out["context_source"] == "shared_refresh"
    assert out["locked_shares_by_symbol"] == {"NVDA": 100.0, "0700.HK": 100.0}


@pytest.mark.parametrize("source", ["account", "shared"])
def test_trusted_to_untrusted_never_serves_old_capacity(ledger, source):
    ledger.advance_a()
    ledger.seed(source)
    ledger.repo.upsert_trade_event(_event(4))  # real event/projection mismatch
    assert ledger.snapshot()["snapshot_status"] != "trusted"
    out, refreshed = ledger.load()
    assert refreshed is True and out["context_source"] == "shared_refresh"
    assert out["locked_shares_status"] == "unavailable"
    assert out["decision_state_fingerprint"] is None


@pytest.mark.parametrize("source", ["account", "shared"])
def test_other_account_commit_preserves_canonical_global_fingerprint_scope(ledger, source):
    ledger.advance_a()
    old = ledger.context()
    ledger.seed(source)
    run_position_projection_forced_full(ledger.repo, [_event(6, account="beta", symbol="MSFT")])
    current = ledger.context()
    assert current["locked_shares_by_symbol"] == old["locked_shares_by_symbol"]
    assert current["decision_state_fingerprint"] != old["decision_state_fingerprint"]
    out, refreshed = ledger.load()
    assert refreshed is True and out["context_source"] == "shared_refresh"
    assert out["decision_state_fingerprint"] == current["decision_state_fingerprint"]
    assert out["locked_shares_by_symbol"] == {"NVDA": 100.0, "0700.HK": 100.0}


def test_stale_empty_cache_refreshes_and_aggregate_still_rebuilds(ledger):
    ledger.seed("account")
    ledger.advance_a()
    ledger.advance_b()
    out, refreshed = ledger.load()
    assert refreshed is True
    _assert_b(out, ledger.context())
    # Different account cache path avoids using the first call's publication.
    out, refreshed = ledger.load(account=None, state_dir=ledger.root / "aggregate")
    assert refreshed is True and out["context_source"] == "shared_refresh"
    assert out["locked_shares_by_symbol"]["MSFT"] == 300.0


@pytest.mark.parametrize("direct_fails", [False, True])
def test_mismatch_preserves_direct_fallback_and_unavailable(ledger, monkeypatch, direct_fails):
    ledger.advance_a()
    ledger.seed("account")
    ledger.advance_b()
    original = pc._load_option_position_records
    calls = []

    def fetch(*args):
        calls.append(args)
        if len(calls) == 1 or direct_fails:
            raise RuntimeError("synthetic records unavailable")
        return original(*args)

    monkeypatch.setattr(pc, "_load_option_position_records", fetch)
    out, refreshed = ledger.load()
    assert len(calls) == 2
    if direct_fails:
        assert out is None and refreshed is False
        assert not ledger.observations
    else:
        assert refreshed is True and out["context_source"] == "direct_fetch"
        _assert_b(out, ledger.context())


def test_both_cache_candidates_share_one_current_observation(ledger, monkeypatch):
    ledger.advance_a()
    ledger.seed("account")
    ledger.seed("shared")
    ledger.advance_b()
    original = pc.decision_state_snapshot
    calls_before_refresh = []
    refreshing = False
    original_records = pc._load_option_position_records

    def observe(*args, **kwargs):
        if not refreshing:
            calls_before_refresh.append(kwargs)
        return original(*args, **kwargs)

    def records(*args):
        nonlocal refreshing
        refreshing = True
        return original_records(*args)

    monkeypatch.setattr(pc, "decision_state_snapshot", observe)
    monkeypatch.setattr(pc, "_load_option_position_records", records)
    out, refreshed = ledger.load()
    assert refreshed is True and out["context_source"] == "shared_refresh"
    assert len(calls_before_refresh) == 1
    _assert_b(out, ledger.context())


def test_unprepared_pipeline_assembly_refreshes_current_capacity(ledger, monkeypatch):
    ledger.advance_a()
    ledger.seed("account")
    ledger.advance_b()
    _, out, *_ = pc.build_pipeline_context(**ledger.outer_args(monkeypatch))
    assert out["context_source"] == "shared_refresh"
    _assert_b(out, ledger.context())


@pytest.mark.parametrize("bad_hash", [False, True])
def test_prepared_pipeline_reader_keeps_frozen_authority_without_ttl_fallback(ledger, monkeypatch, bad_hash):
    ledger.advance_a()
    args = ledger.outer_args(monkeypatch)
    snapshot = ledger.snapshot()
    context = ledger.context()
    run_id = "synthetic-prepared"
    config_hash = hashlib.sha256(json.dumps(args["cfg"], sort_keys=True).encode()).hexdigest()
    authority = {
        "schema_version": "prepared_option_positions_context", "run_id": run_id, "account": "alpha",
        "account_config_sha256": config_hash,
        "ledger_generation_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        "fx_observation_sha256": hashlib.sha256(json.dumps(RATES, sort_keys=True).encode()).hexdigest(),
        "run_fx_snapshot_sha256": None, "fx_status": "unavailable",
        "source_observed_at": snapshot["source_observed_at"],
    }
    context.update(prepared_authority=authority, current_decision_read=snapshot["current_decision_read"],
                   current_decision_shadow=snapshot["current_decision_shadow"],
                   decision_snapshot_actionable=bool(snapshot.get("actionable")), context_source="prepared")
    root = ledger.root / "output_runs" / run_id / "accounts/alpha/state"
    payload = _write(root / "option_positions_context.json", context)
    manifest = _write(root / "prepared_option_positions_context.json", {
        **authority, "status": "ready", "payload_relpath": payload.name,
        "payload_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "decision_state_fingerprint": context["decision_state_fingerprint"],
    })
    ledger.advance_b()
    assert context["decision_state_fingerprint"] != ledger.snapshot()["decision_state_fingerprint"]
    monkeypatch.setattr(pc, "load_option_positions_context", lambda **_kw: pytest.fail("prepared must not use TTL loader"))
    args.update(prepared_option_positions_context_manifest=manifest,
                prepared_option_positions_context_run_id=run_id,
                prepared_option_positions_context_account_config_sha256=config_hash,
                prepared_option_positions_context_manifest_sha256=(
                    "f" * 64 if bad_hash else hashlib.sha256(manifest.read_bytes()).hexdigest()))
    if bad_hash:
        with pytest.raises(PreparedOptionPositionsContextError, match="generation mismatch"):
            pc.build_pipeline_context(**args)
    else:
        _, out, *_ = pc.build_pipeline_context(**args)
        assert out["context_source"] == "prepared"
        assert out["decision_state_fingerprint"] == context["decision_state_fingerprint"]
        assert out["locked_shares_by_symbol"] == {"NVDA": 100.0, "0700.HK": 100.0}


@pytest.mark.parametrize("source", ["account", "shared"])
def test_native_late_publication_after_invalidation_is_rejected_on_next_load(ledger, monkeypatch, source):
    """Controlled A-build -> B-commit/invalidate -> A-publish interleaving.

    Uses actual cache replacement/readback and invalidation; run only where
    native temporary-file publication and fixture deletion are permitted.
    """
    ledger.advance_a()
    old = ledger.context()
    old_account = ledger.seed("account")
    old_shared = ledger.seed("shared")
    native_write = pc.atomic_write_json
    invalidations = []

    def publish_after_b(path, payload, **kwargs):
        if not invalidations:
            assert payload["by_account"]["alpha"]["decision_state_fingerprint"] == old["decision_state_fingerprint"]
            ledger.advance_b()
            receipt = invalidate_option_positions_context_cache(runtime_root=ledger.root, account="alpha")
            invalidations.append(receipt)
            assert receipt["ok"] and set(receipt["invalidated_paths"]) == {str(old_account), str(old_shared)}
            assert not old_account.exists() and not old_shared.exists()
        native_write(path, payload, **kwargs)

    monkeypatch.setattr(pc, "atomic_write_json", publish_after_b)
    first, refreshed = ledger.load(ttl_sec=0)
    assert refreshed is True and first["decision_state_fingerprint"] == old["decision_state_fingerprint"]
    assert json.loads(old_account.read_text())["decision_state_fingerprint"] == old["decision_state_fingerprint"]
    assert json.loads(old_shared.read_text())["by_account"]["alpha"]["decision_state_fingerprint"] == old["decision_state_fingerprint"]
    monkeypatch.setattr(pc, "atomic_write_json", native_write)
    overrides = {} if source == "account" else {"state_dir": ledger.root / "next-account"}
    out, refreshed = ledger.load(**overrides)
    assert refreshed is True and out["context_source"] == "shared_refresh"
    _assert_b(out, ledger.context())
    path = overrides.get("state_dir", ledger.args["state_dir"]) / "option_positions_context.json"
    _assert_b(json.loads(path.read_text()), ledger.context())
    assert len(invalidations) == 1
