"""Real daily planning and owners; only provider transports are synthetic."""

from datetime import datetime, timezone
import json

import pandas as pd
import pytest

from src.application.research import market_ancillary as capture
from src.application.research import watchlist_day as daily, watchlist_series as group
from src.application.current_fx_run import _decode
from src.infrastructure import exchange_rates as fx
from test_watchlist_research_day import calendar_job, job, setup, freeze, write  # noqa: F401
from test_watchlist_research_recurring import recurring  # noqa: F401


@pytest.fixture
def acquisition(calendar_job, monkeypatch):
    args, request, state = calendar_job
    root = args["request_file"].parent
    policy = {
        "schema_version": capture.POLICY_SCHEMA,
        "lead_seconds": 60,
        "max_calls": 5,
        "max_bytes": 100_000_000,
        "min_free_bytes": 1,
    }
    request["ancillary_capture"] = write(root, "ancillary-policy.json", policy)
    window = json.loads((root / request["observation_window"]["path"]).read_bytes())
    window["intervals"][0]["start_utc"] = "2026-10-30T19:54:00Z"
    request["observation_window"] = write(root, "quote-window.json", window)
    freeze(args, request)
    base = args["gateway_factory"]

    class Gateway:
        def __init__(self):
            self.gateway = base()

        def __getattr__(self, key):
            return getattr(self.gateway, key)

        def ensure_quote_ready(self):
            state.calls.append(("preflight",))
            return {"program_status_type": "READY"}

        def get_rehab(self, **kwargs):
            state.calls.append(("rehab", kwargs["code"]))
            return pd.DataFrame([{"ex_div_date": "2026-10-29", "cash_div": 0.1}])

    args["gateway_factory"] = lambda **_: Gateway()
    state.http = []
    state.http_failure = False
    state.late_http = False
    state.interrupted = False
    state.action_rows = [{"ex_div_date": "2026-10-29", "cash_div": 0.1}]

    def http(url, **kwargs):
        state.http.append(url)
        if state.interrupted:
            raise KeyboardInterrupt()
        if state.late_http:
            state.now = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)
        if state.http_failure:
            raise TimeoutError("synthetic network failure")
        return (
            (
                'v_whUSDCNY="310~name~USDCNY~7.2~0~20261030235400~";\n'
                'v_whHKDCNY="310~name~HKDCNY~0.92~0~20261030235400~";\n'
            )
            if "gtimg" in url
            else ""
        )

    monkeypatch.setattr(fx, "_http_get", http)
    monkeypatch.setattr(fx, "_utc_now", lambda: state.now)
    return args, request, state


def at_capture(state):
    state.now = datetime(2026, 10, 30, 19, 54, 1, tzinfo=timezone.utc)


def test_daily_automatic_sources_before_quotes_export_originals(acquisition):
    args, _, state = acquisition
    preview = daily.run_watchlist_day(**args)
    assert preview["status"] == "preview" and not state.calls and not state.http
    daily.run_watchlist_day(**args, confirm=True)
    assert not (args["day_dir"] / "archive").exists()
    at_capture(state)
    result = daily.run_watchlist_day(**args, confirm=True)
    assert result["ancillary_capture"]["status"] == "captured" and len(state.http) == 2
    assert [c[0] for c in state.calls].count("rehab") == 1
    assert not (args["day_dir"] / "archive").exists()
    originals = args["day_dir"] / "market-ancillary"
    frozen = {p.name: p.read_bytes() for p in originals.glob("*.json")}
    daily.run_watchlist_day(**args, confirm=True)
    assert len(state.http) == 2
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    result = daily.run_watchlist_day(**args, confirm=True)
    assert len(state.http) == 2 and [c[0] for c in state.calls].count("snapshot") == 2
    out = args["day_dir"].parent / "export-with-automatic-ancillary"
    exported = group.export_watchlist_series(
        series_dir=args["day_dir"] / "archive",
        expected_plan_sha256=result["manifest"]["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        output_dir=out,
    )
    for batch in exported["batches"]:
        destination = out / "exports" / batch["batch_id"] / "inputs/ancillary/captured"
        assert all((destination / name).read_bytes() == raw for name, raw in frozen.items())
    assert all(p.read_bytes() == frozen[p.name] for p in originals.glob("*.json"))
    native = json.loads(frozen["current_exchange_rates.v1.json"])
    decoded, _ = _decode(frozen["current_exchange_rates.v1.json"], run_id=native["run_id"])
    assert set(fx.verified_exchange_rate_pairs(decoded, observed_at=state.now)) == {"USDCNY", "HKDCNY"}
    assert native["snapshot"]["evaluated_at_utc"] == "2026-10-30T19:54:01+00:00"
    assert not (originals / "unused-cache.json").exists()


@pytest.mark.parametrize("change", ["too_small", "bad_policy", "window_not_covered"])
def test_invalid_capture_contract_never_runs_supplemental(acquisition, change):
    args, request, state = acquisition
    root = args["request_file"].parent
    policy = json.loads((root / "ancillary-policy.json").read_bytes())
    if change == "too_small":
        policy["max_calls"] = 1
    elif change == "bad_policy":
        policy["lead_seconds"] = True
    else:
        policy["lead_seconds"] = 120
    request["ancillary_capture"] = write(root, "ancillary-policy.json", policy)
    freeze(args, request)
    with pytest.raises(ValueError):
        daily.run_watchlist_day(**args, confirm=True)
    assert not state.http and not any(c[0] == "rehab" for c in state.calls)


@pytest.mark.parametrize("failure", ["transport", "late", "missed", "abandoned"])
def test_failed_or_missed_supplemental_never_backfills_or_blocks_quotes(acquisition, failure):
    args, _, state = acquisition
    daily.run_watchlist_day(**args, confirm=True)
    at_capture(state)
    if failure == "transport":
        state.http_failure = True
    elif failure == "late":
        state.late_http = True
    elif failure == "abandoned":
        capture.series._private_dir(args["day_dir"] / "market-ancillary")
    else:
        state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    before = len(state.http)
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    assert len(state.http) == before
    assert [c[0] for c in state.calls].count("snapshot") == 2
    if failure in {"missed", "abandoned"}:
        assert before == 0
    if failure == "late":
        assert not any(c[0] == "rehab" for c in state.calls)


def test_recurring_propagates_frozen_capture_policy(acquisition):
    args, request, state = acquisition
    root = args["request_file"].parent
    schedule = {
        "schema_version": daily.ARCHIVING_SCHEMA,
        "start_date": "2026-10-30",
        "timezone": "America/New_York",
        "calendar_markets": ["US"],
        "inputs": {
            k: request[k]
            for k in ("scan_input", "policy", "discovery_budget", "observation_budget", "ancillary_capture")
        },
        "discovery_windows": [{"timezone": "America/New_York", "start_local": "09:00", "end_local": "12:00"}],
        "observation_windows": [{"timezone": "America/New_York", "start_local": "15:54", "end_local": "15:56"}],
    }
    ref = write(root, "schedule-ancillary.json", schedule)
    options = {k: v for k, v in args.items() if k not in {"request_file", "expected_request_sha256", "day_dir"}}
    options.update(
        schedule_file=root / ref["path"],
        expected_schedule_sha256=ref["sha256"],
        archive_root=args["day_dir"].parent / "auto-daily",
    )
    assert daily.run_watchlist_schedule(**options)["provider_called"] is False
    daily.run_watchlist_schedule(**options, confirm=True)
    at_capture(state)
    daily.run_watchlist_schedule(**options, confirm=True)
    assert len(state.http) == 2 and [c[0] for c in state.calls].count("rehab") == 1
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_schedule(**options, confirm=True)
    assert [c[0] for c in state.calls].count("snapshot") == 2


def test_interrupted_source_is_abandoned_without_replay(acquisition):
    args, _, state = acquisition
    daily.run_watchlist_day(**args, confirm=True)
    at_capture(state)
    state.interrupted = True
    with pytest.raises(KeyboardInterrupt):
        daily.run_watchlist_day(**args, confirm=True)
    assert len(state.http) == 1
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    daily.run_watchlist_day(**args, confirm=True)
    assert len(state.http) == 1 and [c[0] for c in state.calls].count("snapshot") == 2


def test_changed_captured_original_is_rejected_before_quotes(acquisition):
    args, _, state = acquisition
    daily.run_watchlist_day(**args, confirm=True)
    at_capture(state)
    daily.run_watchlist_day(**args, confirm=True)
    target = args["day_dir"] / "market-ancillary/fx-provider-0.json"
    target.write_bytes(target.read_bytes() + b" ")
    state.now = datetime(2026, 10, 30, 19, 55, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        daily.run_watchlist_day(**args, confirm=True)
    assert not any(c[0] == "snapshot" for c in state.calls)


@pytest.mark.parametrize("rows", [[], [{"ex_div_date": "invalid", "cash_div": 0.1}]])
def test_corporate_action_original_retained_without_certifying_coverage(acquisition, rows):
    args, _, state = acquisition
    base = args["gateway_factory"]

    class Gateway:
        def __init__(self):
            self.gateway = base()

        def __getattr__(self, key):
            return getattr(self.gateway, key)

        def get_rehab(self, **kwargs):
            return rows

    args["gateway_factory"] = lambda **_: Gateway()
    daily.run_watchlist_day(**args, confirm=True)
    at_capture(state)
    daily.run_watchlist_day(**args, confirm=True)
    files = list((args["day_dir"] / "market-ancillary").glob("*.corporate_actions.json"))
    assert len(files) == 1
    raw = json.loads(files[0].read_bytes())
    assert raw["responses"] == [{"rows": rows}]
    assert raw["status"] == ("received_unverified_coverage" if not rows else "failed")
