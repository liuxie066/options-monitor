from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import types

import pandas as pd
import pytest

from src.application.reports.strike_metrics import unavailable_evidence

ROOT = Path(__file__).resolve().parents[1]


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/launchd" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver():
    return _module("weekly_driver", "run-config-review-weekly.py")


def _configs(root):
    configs = root / "configs"
    configs.mkdir()
    (configs / "user.hk.json").write_text('{"symbols":[]}')
    (configs / "user.us.json").write_text('{"symbols":[{"symbol":"NVDA","sell_put":{"max_strike":100}}]}')
    (configs / "system.json").write_text("{}")
    return configs


@pytest.mark.parametrize("failure", [None, "timeout", "bad_json", "invalid_price"])
def test_weekly_entry_persists_real_report_and_never_reuses_old_files(tmp_path, monkeypatch, driver, failure):
    configs = _configs(tmp_path)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    old = inputs / "prices.current.json"
    old.write_text('{"prices":{"NVDA":999999},"generated_at_utc":"2099-01-01T00:00:00Z"}')
    env_file = tmp_path / "empty.env"
    env_file.write_text("")
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("PYTHONPATH", "invalid-ambient-import-root")
    real_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "ssh":
            assert kwargs["timeout"] == 480
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if failure == "bad_json":
                return subprocess.CompletedProcess(command, 0, "not JSON", "")
            args = shlex.split(command[-1])
            if "--as-of" in args:
                now = datetime.fromisoformat(args[args.index("--as-of") + 1])
                payload = unavailable_evidence(now, "fixture_direction_history_unavailable")
            else:
                payload = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                           "prices": {"NVDA": -1 if failure == "invalid_price" else 115}}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        assert kwargs["timeout"] == 600 and "PYTHONPATH" not in kwargs["env"]
        return real_run(command, **kwargs, capture_output=True, text=True)

    monkeypatch.setattr(driver.subprocess, "run", run)
    argv = ["report", "run", "config_review", "--runtime-root", str(tmp_path),
            "--configs-root", str(configs), "--trigger", "schedule", "--no-deliver", "--json"]
    assert driver.run_weekly(argv, inputs_root=inputs, remote_host="fixture-host",
                             remote_repo_root="/fixture/release", remote_runtime_root="/fixture/runtime") == 0
    receipts = list(inputs.glob("*/prefetch.json"))
    assert len(receipts) == 1
    prefetch = json.loads(receipts[0].read_text())
    assert prefetch["report"]["exit_code"] == 0
    assert prefetch["prices"]["status"] == ("fetched" if failure is None else "failed")
    assert prefetch["evidence"]["status"] == ("failed" if failure in {"timeout", "bad_json"} else "fetched")
    reports = list((tmp_path / "reports/config_review").glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["conclusion"]["state"] == "观察"
    assert report["strike_rejections"]["count"] is None
    text = reports[0].with_name("report.md").read_text()
    assert "999999" not in text
    assert ("115.00" in text) == (failure is None)
    run_receipt = json.loads(reports[0].with_name("run.json").read_text())
    assert run_receipt["delivery"]["status"] == "skipped"
    assert str(old) not in prefetch["report"]["command"]
    assert old.exists()
    assert len(calls) == 3


def test_fetch_uses_shared_exporter_and_safe_remote_arguments(tmp_path, monkeypatch, driver):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    actual_run = subprocess.run
    remote_repo = "/fixture/release path;literal"

    def run(command, **kwargs):
        assert command[0] == "ssh" and command[-2] == "fixture-host"
        assert f"cd {shlex.quote(remote_repo)} && exec" in command[-1]
        assert "PYTHONDONTWRITEBYTECODE=1" in command[-1]
        # Execute the exact source supplied to SSH, using real local owner APIs.
        return actual_run([sys.executable, "-", "--export-root", str(runtime), "--as-of", now],
                          cwd=ROOT, input=kwargs["input"], text=True, capture_output=True,
                          timeout=kwargs["timeout"], check=True)

    monkeypatch.setattr(driver.subprocess, "run", run)
    payload = driver._fetch_json(ROOT / "src/application/reports/strike_metrics.py",
                                 ["--export-root", str(runtime), "--as-of", now],
                                 host="fixture-host", repo_root=remote_repo)
    assert payload["schema_version"] == "config_review_candidate_evidence.v1"
    assert payload["snapshots"] == [] and payload["source"]["release_root"] == str(ROOT)
    assert list(runtime.iterdir()) == []


@pytest.mark.parametrize("provider_failure", [False, True])
def test_price_helper_uses_canonical_codes_and_closes_context(monkeypatch, provider_failure):
    helper = _module("weekly_prices", "fetch-config-review-prices.py")
    calls = []

    class Context:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_market_snapshot(self, codes):
            calls.append(codes)
            if provider_failure:
                return -1, "unavailable"
            return 0, pd.DataFrame([{"code": "HK." + "00700", "last_price": 400},
                                    {"code": "US.NVDA", "last_price": 115}])

        def close(self):
            calls.append("closed")

    monkeypatch.setitem(sys.modules, "futu", types.SimpleNamespace(RET_OK=0, OpenQuoteContext=Context))
    if provider_failure:
        with pytest.raises(RuntimeError, match="snapshot request failed"):
            helper.fetch_prices(["0700.HK", "NVDA"])
    else:
        payload = helper.fetch_prices(["0700.HK", "NVDA"])
        assert payload["prices"] == {"0700.HK": {"price": 400}, "NVDA": {"price": 115}}
        assert datetime.fromisoformat(payload["generated_at_utc"]).tzinfo is not None
    assert calls[1] == ["HK." + "00700", "US.NVDA"] and calls[-1] == "closed"


def test_weekly_report_timeout_has_a_persisted_terminal_receipt(tmp_path, monkeypatch, driver):
    configs = _configs(tmp_path)
    monkeypatch.setattr(driver, "_fetch_json", lambda *a, **k: (_ for _ in ()).throw(ValueError("offline")))

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(driver.subprocess, "run", timeout)
    assert driver.run_weekly(["report", "run", "config_review", "--configs-root", str(configs), "--no-deliver"],
                             inputs_root=tmp_path / "inputs", remote_host="fixture",
                             remote_repo_root="/fixture", remote_runtime_root="/runtime") == 124
    receipt = json.loads(next((tmp_path / "inputs").glob("*/prefetch.json")).read_text())
    assert receipt["report"]["status"] == "timeout"
    assert 2 * driver.FETCH_TIMEOUT_SECONDS + driver.REPORT_TIMEOUT_SECONDS < 1800


def test_plist_and_shell_templates_render_with_weekly_runner(tmp_path):
    template = ROOT / "deploy/launchd/com.options-monitor.report.config-review.weekly.plist.template"
    text = template.read_text()
    values = {"OM_BIN": "/fixture/app support/run-weekly.sh", "RUNTIME_ROOT": "/fixture/runtime",
              "CONFIGS_ROOT": "/fixture/configs", "ENV_FILE": "/fixture/credentials.env",
              "LOG_DIR": "/fixture/logs", "HOME": "/fixture/home", "INPUTS_ROOT": "/fixture/inputs",
              "REMOTE_HOST": "fixture-host", "REMOTE_REPO_ROOT": "/fixture/release", "REMOTE_RUNTIME_ROOT": "/fixture/runtime"}
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    assert "__" not in text
    plist = plistlib.loads(text.encode())
    assert plist["StartCalendarInterval"] == {"Weekday": 1, "Hour": 8, "Minute": 0}
    assert plist["ProgramArguments"][0] == values["OM_BIN"]
    assert plist["EnvironmentVariables"]["OM_ENV_FILE"] == values["ENV_FILE"]
    script = (ROOT / "deploy/launchd/run-config-review-weekly.sh.template").read_text()
    script = script.replace("__SOURCE_ROOT__", "/fixture/app support/source").replace("__PYTHON__", sys.executable)
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0
