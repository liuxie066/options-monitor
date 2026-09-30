import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256
from src.application.config_defaults import DEFAULT_CONFIG
from src.application.config_validator import validate_config
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.config_yaml_holdings import set_yaml_holdings_inclusion
from src.infrastructure.portfolio_management_client import API_VERSION, PortfolioManagementClient
import src.application.config_yaml_holdings as inclusion


REPO_ROOT = Path(__file__).resolve().parents[1]


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "config.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "accounts": {"lx": {"type": "futu", "futu_account_id": "12345678"}},
                "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return source


def _set(source: Path, enabled: bool, **kwargs):
    return set_yaml_holdings_inclusion(
        repo_root=REPO_ROOT,
        enabled=enabled,
        config_path=source,
        runtime_root=source.parent,
        **kwargs,
    )


def test_holdings_default_is_disabled_in_system_and_market_snapshot(tmp_path: Path) -> None:
    source = _source(tmp_path)
    assert DEFAULT_CONFIG["defaults"]["portfolio"]["holdings"]["enabled"] is False
    market, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=source)
    assert market["portfolio"]["holdings"]["enabled"] is False


@pytest.mark.parametrize("setting", (None, {"enabled": "true"}, {"enabled": False, "extra": 1}))
def test_holdings_setting_rejects_invalid_shape(tmp_path: Path, setting) -> None:
    source = _source(tmp_path)
    market, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=source)
    market["portfolio"]["holdings"] = setting
    with pytest.raises(SystemExit, match="portfolio.holdings"):
        validate_config(market)


def test_holdings_enable_requires_probe_preview_and_readback(monkeypatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    probes = []
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda cfg: probes.append(cfg) or {
        "status": "ready_observed", "scope": "observed_only", "accounts_observed": ["lx"]
    })
    preview = _set(source, True)
    assert preview["dry_run"] is True
    assert preview["source_revision"]["before_sha256"] == config_source_sha256(source)
    assert preview["current_enabled"] is False
    assert preview["target_enabled"] is True
    assert preview["preview_sha256"]
    assert preview["effect_scope"] == "configuration_only"
    assert preview["audit_id"] is None
    assert preview["rollback_hint"] is None
    assert "holdings" not in yaml.safe_load(source.read_text())["accounts"]["lx"]
    assert not (tmp_path / "config.us.json").exists()
    assert len(probes) == 1

    with pytest.raises(AgentToolError, match="before_sha256"):
        _set(source, True, apply=True)
    result = _set(source, True, apply=True, confirm=True,
                  expected_source_sha256=preview["source_revision"]["before_sha256"],
                  expected_preview_sha256=preview["preview_sha256"])
    assert result["write_applied"] is True
    assert "config build" in result["rollback_hint"]
    assert "config build-assistant" in result["rollback_hint"]
    assert set(result["verified_targets"]) == {
        str(source), str(tmp_path / "config.us.json"),
        str(tmp_path / "resolved" / "config.assistant.json"),
    }
    assert yaml.safe_load(source.read_text())["portfolio"]["holdings"]["enabled"] is True
    assert (tmp_path / "config.us.json").is_file()

    monkeypatch.setattr(inclusion, "_probe_holdings", lambda _cfg: (_ for _ in ()).throw(ValueError("PM down")))
    off_preview = _set(source, False)
    _set(source, False, apply=True, confirm=True,
         expected_source_sha256=off_preview["source_revision"]["before_sha256"],
         expected_preview_sha256=off_preview["preview_sha256"])
    assert yaml.safe_load(source.read_text())["portfolio"]["holdings"]["enabled"] is False
    assert len(probes) == 2


def test_holdings_post_commit_readback_failure_reports_write_receipt(monkeypatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda _cfg: {"status": "ready_observed"})
    preview = _set(source, True)
    publish = inclusion.publish_yaml_config_generation

    def corrupt_assistant_after_publish(**kwargs):
        result = publish(**kwargs)
        if kwargs["apply"]:
            Path(result["assistant"]["output_config_path"]).write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(inclusion, "publish_yaml_config_generation", corrupt_assistant_after_publish)
    with pytest.raises(AgentToolError) as caught:
        _set(source, True, apply=True, confirm=True,
             expected_source_sha256=preview["source_revision"]["before_sha256"],
             expected_preview_sha256=preview["preview_sha256"])
    error = caught.value
    assert error.code == "CONFIG_READBACK_FAILED"
    assert error.details["write_applied"] is True
    assert error.details["audit_id"]
    assert error.details["backup_path"]
    assert error.details["target"] == str(tmp_path / "resolved" / "config.assistant.json")
    assert yaml.safe_load(source.read_text())["portfolio"]["holdings"]["enabled"] is True


def test_holdings_failed_preflight_does_not_write(monkeypatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda _cfg: (_ for _ in ()).throw(ValueError("stale")))
    preview = _set(source, True)
    assert preview["preflight"]["status"] == "failed"
    with pytest.raises(AgentToolError, match="Holdings source is not ready"):
        _set(source, True, apply=True, confirm=True,
             expected_source_sha256=preview["source_revision"]["before_sha256"],
             expected_preview_sha256=preview["preview_sha256"])
    assert source.read_bytes() == before
    assert not (tmp_path / "config.us.json").exists()


def test_holdings_confirmation_binds_target(monkeypatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda _cfg: {"status": "ready_observed"})
    preview = _set(source, True)
    with pytest.raises(AgentToolError, match="preview"):
        _set(source, False, apply=True, confirm=True,
             expected_source_sha256=preview["source_revision"]["before_sha256"],
             expected_preview_sha256=preview["preview_sha256"])
    assert source.read_bytes() == before


@pytest.mark.parametrize("account_warnings", (None, ["cash flow is stale"]))
def test_holdings_probe_reports_observed_scope_and_rejects_stale(monkeypatch, account_warnings) -> None:
    class Client:
        def read_view(self, view, *, query, timeout):
            assert view == "accounts"
            assert query == {"include_default": "false"}
            assert timeout == 10.0
            result = {
                "success": True,
                "accounts": ["lx"],
                "count": 1,
                "retrieved_at_utc": "2026-09-29T00:00:00Z",
                "freshness": {"status": "fresh", "trust_status": "trusted"},
            }
            if account_warnings is not None:
                result["warnings"] = account_warnings
            return result

    monkeypatch.setattr(inclusion, "resolve_portfolio_management_client", lambda *_args, **_kwargs: Client())
    monkeypatch.setattr(inclusion, "read_portfolio_valuation_evidence", lambda **_kwargs: {
        "status": "complete", "warnings": [],
        "freshness": {"status": "fresh", "trust_status": "trusted", "observed_at_utc": "2026-09-29T00:00:00Z"},
        "account_status": [{"account": "lx", "status": "complete"}],
        "holdings": [{"account": "lx", "code": "NVDA", "broker": "富途"}],
    })
    scope = inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})
    assert scope["accounts_observed"] == ["lx"]
    assert scope["source_observed_at"] == "2026-09-29T00:00:00Z"
    assert scope["brokers_observed"] == ["富途"]
    assert scope["markets_observed"] == ["US"]

    monkeypatch.setattr(inclusion, "read_portfolio_valuation_evidence", lambda **_kwargs: {
        "status": "complete", "warnings": [],
        "freshness": {"status": "fresh", "trust_status": "trusted"},
        "account_status": [{"account": "lx", "status": "complete"}],
        "holdings": [],
    })
    with pytest.raises(ValueError, match="no observed holdings"):
        inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})

    monkeypatch.setattr(inclusion, "read_portfolio_valuation_evidence", lambda **_kwargs: {
        "status": "partial", "warnings": ["stale"], "freshness": {"status": "stale"},
        "account_status": [], "holdings": [],
    })
    with pytest.raises(ValueError, match="incomplete or stale"):
        inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})


def test_holdings_probe_accepts_contract_response_through_pm_client(monkeypatch) -> None:
    seen = []

    class Response:
        status = 200
        headers = {"X-PM-API-Version": API_VERSION}

        def __init__(self, payload):
            self.body = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return self.body

    freshness = {
        "status": "fresh", "trust_status": "trusted",
        "observed_at_utc": "2026-09-29T00:00:00Z",
        "dataset_ids": ["pm.holdings_quantity"], "reason_codes": [],
    }

    def open_pm(request, *, timeout):
        seen.append((request.method, request.full_url, timeout))
        if request.method == "GET":
            return Response({
                "success": True, "freshness": freshness,
                "retrieved_at_utc": "2026-09-29T00:00:01Z",
                "accounts": ["hb", "lx"], "count": 2,
            })
        assert json.loads(request.data)["accounts"] == ["lx"]
        return Response({
            "success": True, "freshness": freshness,
            "retrieved_at_utc": "2026-09-29T00:00:01Z",
            "schema_version": "portfolio.valuation_evidence.v1",
            "status": "complete", "scope": {"accounts": ["lx"]},
            "snapshot": {"snapshot_id": "valuation-1", "observed_at": "2026-09-29T00:00:00Z"},
            "holdings": [{"account": "lx", "broker": "富途", "code": "NVDA"}],
            "quotes": [], "account_status": [{"account": "lx", "status": "complete"}],
            "warnings": [],
        })

    monkeypatch.setattr(
        inclusion, "resolve_portfolio_management_client",
        lambda *_args, **_kwargs: PortfolioManagementClient(urlopen_fn=open_pm),
    )
    scope = inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})
    assert scope["accounts_observed"] == ["lx"]
    assert scope["markets_observed"] == ["US"]
    assert [method for method, _url, _timeout in seen] == ["GET", "POST"]


def test_holdings_probe_rejects_missing_om_account(monkeypatch) -> None:
    class Client:
        def read_view(self, _view, *, query, timeout):
            return {
                "success": True,
                "accounts": ["hb"],
                "freshness": {"status": "fresh", "trust_status": "trusted"},
            }

    monkeypatch.setattr(inclusion, "resolve_portfolio_management_client", lambda *_args, **_kwargs: Client())
    with pytest.raises(ValueError, match="missing configured OM accounts: lx"):
        inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})


def test_holdings_switch_cannot_be_overridden_per_market(tmp_path: Path) -> None:
    source = _source(tmp_path)
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    doc["markets"]["us"]["portfolio"] = {"holdings": {"enabled": True}}
    source.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(AgentToolError, match="must be configured globally"):
        _set(source, False)


def test_holdings_cli_preview_and_apply_off_in_isolated_runtime(tmp_path: Path) -> None:
    source = _source(tmp_path)

    def run(*args: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-m", "src.interfaces.cli.main", "config", "holdings", "set",
             "--enabled", "false", "--config-yaml", str(source), "--runtime-root", str(tmp_path), *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)

    preview = run()
    assert preview["dry_run"] is True
    assert preview["audit_id"] is None
    assert not (tmp_path / "config.us.json").exists()
    applied = run(
        "--apply", "--confirm",
        "--expected-source-sha256", preview["source_revision"]["before_sha256"],
        "--expected-preview-sha256", preview["preview_sha256"],
    )
    assert applied["write_applied"] is True
    assert applied["effect_scope"] == "configuration_only"
    assert (tmp_path / "config.us.json").is_file()
