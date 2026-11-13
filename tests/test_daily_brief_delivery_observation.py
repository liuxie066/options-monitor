from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from decision_history_fixtures import delete_history_revision, replace_history_payload
from src.application import daily_decision_brief_repository as repo
from test_daily_decision_brief_repository_v2 import (
    MARKET_DATE, _confirmed_repeated_source, _read_state, _prepare,
)


def test_unchanged_day_reuses_validation_but_observes_sources_every_time(tmp_path):
    _confirmed_repeated_source(tmp_path)
    normalizer = repo._normalize_delivery_state
    history = repo._history_raw
    with patch.object(repo, "_normalize_delivery_state", wraps=normalizer) as normalized:
        with patch.object(repo, "_history_raw", wraps=history) as observed:
            with repo.daily_brief_delivery_validation_scope():
                first = _read_state(tmp_path)
                first["state"]["days"].clear()
                second = _read_state(tmp_path)
            assert second["available"] and MARKET_DATE in second["state"]["days"]
            assert normalized.call_count == 1
            assert observed.call_count == 2
            with repo.daily_brief_delivery_validation_scope():
                assert _read_state(tmp_path)["available"]
            assert normalized.call_count == 2


def test_external_process_write_is_visible_inside_read_scope(tmp_path):
    _, prepared = _confirmed_repeated_source(tmp_path)
    path = prepared
    scope = repo.DailyBriefReadScope(base=tmp_path, account="lx", market="US")
    assert _read_state(tmp_path, read_scope=scope)["available"]
    # An independent process atomically replaces the file between two observations.
    raw = json.loads(path.read_text())
    raw["days"][MARKET_DATE]["fixed_reports"].clear()
    raw["days"][MARKET_DATE]["alerted_candidates"].clear()
    code = (
        "import os, pathlib, sys; p=pathlib.Path(sys.argv[1]); "
        "q=p.with_suffix('.external'); q.write_text(sys.argv[2]); os.replace(q,p)"
    )
    subprocess.run([sys.executable, "-I", "-c", code, str(path), json.dumps(raw)], check=True)
    second = _read_state(tmp_path, read_scope=scope)
    assert second["available"] and second["state"]["days"][MARKET_DATE]["fixed_reports"] == {}


@pytest.mark.parametrize("fault", ["message", "identity", "source_changed", "source_missing", "database", "schema"])
def test_cached_day_never_authorizes_changed_or_missing_evidence(tmp_path, fault):
    persisted, prepared = _confirmed_repeated_source(tmp_path)
    path = prepared
    with repo.daily_brief_delivery_validation_scope():
        assert _read_state(tmp_path)["available"]
        if fault == "source_changed":
            replace_history_payload(tmp_path, {**persisted["brief"], "strategy_summary": "changed"})
        elif fault == "source_missing":
            delete_history_revision(tmp_path, revision=persisted["current_revision"])
        elif fault == "database":
            from src.infrastructure.decision_history_sqlite import history_path
            history_path(tmp_path).write_bytes(b"corrupted authoritative history")
        else:
            raw = json.loads(path.read_text())
            if fault == "identity":
                raw["account"] = "other"
            elif fault == "schema":
                raw["schema_version"] = "invalid"
            else:
                next(iter(raw["days"][MARKET_DATE]["fixed_reports"].values()))["rendered_message"] = "tampered"
            path.write_text(json.dumps(raw))
        rejected = _read_state(tmp_path)
        assert rejected["available"] is False and rejected["reason"] == "state_invalid"


def test_failed_validation_can_recover_without_leaking_prior_mutation(tmp_path):
    _, prepared = _confirmed_repeated_source(tmp_path)
    path = prepared
    original = path.read_bytes()
    with repo.daily_brief_delivery_validation_scope():
        initial = deepcopy(_read_state(tmp_path))
        path.write_text('{"schema_version": "broken"}')
        assert _read_state(tmp_path)["reason"] == "state_invalid"
        path.write_bytes(original)
        assert _read_state(tmp_path) == initial


def test_writer_lock_reads_fresh_and_attempt_confirmation_are_visible(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from src.application.notification_delivery_adapter import build_notification_transport_key

    persisted, path = _confirmed_repeated_source(tmp_path)
    with repo.daily_brief_delivery_validation_scope():
        assert _read_state(tmp_path)["available"]
        original_lock = repo._exclusive_lock
        external_target = "2026-07-21T11:00:00-04:00"

        @contextmanager
        def replace_before_lock(lock_path):
            raw = json.loads(path.read_text())
            # Simulate a different writer that finished before this lock acquisition.
            raw["days"][MARKET_DATE]["alerted_candidates"].clear()
            path.write_text(json.dumps(raw))
            with original_lock(lock_path):
                yield

        with monkeypatch.context() as patcher:
            patcher.setattr(repo, "_exclusive_lock", replace_before_lock)
            prepared = _prepare(
                tmp_path, run_id=persisted["brief"]["run_id"], delivery_kind="fixed_report",
                source_kind="successful_brief", revision=persisted["current_revision"],
                source_digest=persisted["current_brief_digest"], rendered_message="second fixed report",
                scheduled_target_market=external_target,
                candidate_identities=persisted["current_candidate_identities"],
            )
        assert _read_state(tmp_path)["state"]["days"][MARKET_DATE]["alerted_candidates"] == {}
        envelope = prepared["envelope"]
        identity = dict(base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE,
                        delivery_key=envelope["delivery_key"], source_digest=envelope["source_digest"],
                        message_sha256=envelope["message_sha256"],
                        transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]))
        repo.record_daily_decision_brief_delivery_attempt(**identity, ambiguous=True)
        assert _read_state(tmp_path)["state"]["days"][MARKET_DATE]["fixed_reports"][external_target]["status"] == "ambiguous"
        repo.confirm_daily_decision_brief_delivery_v2(**identity)
        confirmed = _read_state(tmp_path)["state"]["days"][MARKET_DATE]["fixed_reports"][external_target]
        assert confirmed["status"] == "confirmed"
        assert confirmed["delivery_key"] == envelope["delivery_key"]
        assert confirmed["message_sha256"] == envelope["message_sha256"]


def test_notification_exception_discards_validation_scope(tmp_path, monkeypatch):
    from test_daily_decision_brief_notification_flow import _request, _run_flow

    def fail(_request, **_kwargs):
        assert repo._DELIVERY_VALIDATION.get() is not None
        raise OSError("controlled preparation failure")

    monkeypatch.setattr("src.application.multi_account_tick._prepare_daily_brief_notification", fail)
    with pytest.raises(OSError, match="controlled preparation failure"):
        _run_flow(_request(tmp_path, run_id="failed").request)
    assert repo._DELIVERY_VALIDATION.get() is None


def test_full_flow_normalizes_only_changed_days(tmp_path, monkeypatch):
    from daily_brief_history_fixtures import seed_delivery_history
    from test_daily_decision_brief_notification_flow import _patch_assembler, _patch_sender, _request, _run_flow

    seed_delivery_history(tmp_path, days=3)
    _patch_assembler(monkeypatch)
    sends = []
    _patch_sender(monkeypatch, calls=sends)
    day_counts = []
    original = repo._normalize_delivery_state

    def count(raw, **kwargs):
        day_counts.append(len(raw["days"]))
        return original(raw, **kwargs)

    monkeypatch.setattr(repo, "_normalize_delivery_state", count)
    assert _run_flow(_request(tmp_path, run_id="current").request) == 0
    assert day_counts == [3, 1, 1]
    assert len(sends) == 1
    assert repo._DELIVERY_VALIDATION.get() is None


@pytest.mark.parametrize("duplicate", [False, True])
def test_legacy_date_aliases_never_reuse_another_raw_days_hash(tmp_path, duplicate):
    persisted, path = _confirmed_repeated_source(tmp_path)
    raw = json.loads(path.read_text())
    confirmed = raw["days"].pop(MARKET_DATE)
    empty = deepcopy(confirmed)
    empty["fixed_reports"].clear()
    empty["alerted_candidates"].clear()
    if duplicate:
        raw["days"][MARKET_DATE] = empty
    raw["days"][MARKET_DATE + " "] = confirmed
    path.write_text(json.dumps(raw))
    with repo.daily_brief_delivery_validation_scope():
        first = _read_state(tmp_path)
        assert first["available"] and len(first["state"]["days"][MARKET_DATE]["fixed_reports"]) == 1
        raw["days"] = {MARKET_DATE: empty}
        path.write_text(json.dumps(raw))
        second = _read_state(tmp_path)
        assert second["available"] and second["state"]["days"][MARKET_DATE]["fixed_reports"] == {}
        target = "2026-07-21T11:00:00-04:00"
        _prepare(
            tmp_path, run_id=persisted["brief"]["run_id"], delivery_kind="fixed_report",
            source_kind="successful_brief", revision=persisted["current_revision"],
            source_digest=persisted["current_brief_digest"], rendered_message="new fixed report",
            scheduled_target_market=target,
        )
        persisted_day = json.loads(path.read_text())["days"][MARKET_DATE]
        assert set(persisted_day["fixed_reports"]) == {target}


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_legacy_render_metadata_keeps_full_validation_contract(tmp_path, number):
    import math

    persisted, _ = _confirmed_repeated_source(tmp_path)
    target = "2026-07-21T11:00:00-04:00"
    _prepare(
        tmp_path, run_id=persisted["brief"]["run_id"], delivery_kind="fixed_report",
        source_kind="successful_brief", revision=persisted["current_revision"],
        source_digest=persisted["current_brief_digest"], rendered_message="legacy metadata",
        scheduled_target_market=target, render_context={"metric": number},
    )
    with repo.daily_brief_delivery_validation_scope():
        for _ in range(2):
            result = _read_state(tmp_path)
            metric = result["state"]["days"][MARKET_DATE]["fixed_reports"][target]["render_context"]["metric"]
            assert result["available"] and not math.isfinite(metric)


def test_escaped_read_scopes_use_each_current_flow_and_leave_it_empty(tmp_path, monkeypatch):
    _confirmed_repeated_source(tmp_path)
    before = repo.DailyBriefReadScope(base=tmp_path, account="lx", market="US")
    observed = []
    original = repo._DeliveryValidation.normalize

    def normalize(self, *args, **kwargs):
        observed.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(repo._DeliveryValidation, "normalize", normalize)
    with repo.daily_brief_delivery_validation_scope():
        flow_a = repo._DELIVERY_VALIDATION.get()
        inside = repo.DailyBriefReadScope(base=tmp_path, account="lx", market="US")
        assert _read_state(tmp_path, read_scope=before)["available"]
        assert _read_state(tmp_path, read_scope=inside)["available"]
    assert flow_a._states == {}
    with repo.daily_brief_delivery_validation_scope():
        flow_b = repo._DELIVERY_VALIDATION.get()
        assert _read_state(tmp_path, read_scope=before)["available"]
        assert _read_state(tmp_path, read_scope=inside)["available"]
    assert observed == [flow_a, flow_a, flow_b, flow_b]
    assert flow_b._states == {} and before._validation._states == {} and inside._validation._states == {}


@pytest.mark.parametrize("fault", ["changed", "missing"])
def test_cached_v3_recovery_reobserves_its_successful_source(tmp_path, fault):
    from test_daily_decision_brief_recovery_repository import setup_recovery

    _, success, _ = setup_recovery(tmp_path)
    with repo.daily_brief_delivery_validation_scope():
        assert _read_state(tmp_path)["state"]["schema_version"].endswith("v3")
        if fault == "changed":
            replace_history_payload(tmp_path, {**success["brief"], "strategy_summary": "changed"})
        else:
            delete_history_revision(tmp_path, revision=success["current_revision"])
        assert _read_state(tmp_path)["reason"] == "state_invalid"


def test_two_populated_scopes_keep_distinct_delivery_content(tmp_path):
    from test_daily_decision_brief_repository_v2 import _brief

    scopes = [(tmp_path, "lx", "US"), (tmp_path, "sy", "US"),
              (tmp_path, "lx", "HK"), (tmp_path / "other", "lx", "US")]
    for index, (base, account, market) in enumerate(scopes):
        source = repo.persist_daily_decision_brief_success(
            base=base, brief=_brief(run_id=f"isolated-{index}", account=account, market=market),
        )
        repo.prepare_daily_decision_brief_delivery(
            base=base, account=account, market=market, market_trading_date=MARKET_DATE,
            run_id=f"isolated-{index}", delivery_kind="fixed_report", source_kind="successful_brief",
            revision=source["current_revision"], source_digest=source["current_brief_digest"],
            scheduled_target_market="2026-07-21T10:00:00-04:00", rendered_message=f"report-{index}",
        )
    with repo.daily_brief_delivery_validation_scope():
        for _ in range(2):
            for index, (base, account, market) in enumerate(scopes):
                result = _read_state(base, account=account, market=market)
                report = next(iter(result["state"]["days"][MARKET_DATE]["fixed_reports"].values()))
                assert report["rendered_message"] == f"report-{index}"


def test_cached_history_does_not_cross_base_account_or_market(tmp_path):
    _confirmed_repeated_source(tmp_path)
    with repo.daily_brief_delivery_validation_scope():
        assert _read_state(tmp_path)["available"]
        for overrides in ({"account": "other"}, {"market": "HK"}):
            result = _read_state(tmp_path, **overrides)
            assert result["available"] is False and result["reason"] == "not_found"
        assert _read_state(tmp_path / "other-instance")["reason"] == "not_found"
    scope = repo.DailyBriefReadScope(base=tmp_path, account="lx", market="US")
    with pytest.raises(ValueError, match="identity mismatch"):
        _read_state(tmp_path, market="HK", read_scope=scope)
