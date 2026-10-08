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


def test_holdings_preview_binds_explicit_pm_origin(monkeypatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    origins = []
    def probe(_config, *, service_url=None):
        origins.append(service_url)
        return {"status": "ready_empty", "approved_non_futu_brokers": {"lx": []}}
    monkeypatch.setattr(inclusion, "_probe_holdings", probe)
    preview = _set(source, True, service_url="http://127.0.0.1:8765")
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        _set(source, True, service_url="http://127.0.0.1:8766", apply=True, confirm=True,
             expected_source_sha256=preview["source_revision"]["before_sha256"],
             expected_preview_sha256=preview["preview_sha256"])
    assert origins == ["http://127.0.0.1:8765", "http://127.0.0.1:8766"]
    assert not (tmp_path / "config.us.json").exists()


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


def test_holdings_broker_approval_validation_and_legacy_config(tmp_path: Path) -> None:
    source = _source(tmp_path)
    market, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=source)
    market["portfolio"]["holdings"] = {"enabled": True}
    validate_config(market)  # Existing enabled config stays loadable until re-preview.
    market["portfolio"]["holdings"]["approved_non_futu_brokers"] = {"lx": []}
    validate_config(market)
    for invalid in ({}, {"lx": ["银行", "银行"]}, {"lx": [" "]}):
        market["portfolio"]["holdings"]["approved_non_futu_brokers"] = invalid
        with pytest.raises(SystemExit, match="approved_non_futu_brokers"):
            validate_config(market)


def test_holdings_enable_requires_probe_preview_and_readback(monkeypatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    probes = []
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda cfg: probes.append(cfg) or {
        "status": "ready_observed", "scope": "non_futu", "accounts_observed": ["lx"],
        "approved_non_futu_brokers": {"lx": ["银行"]},
    })
    preview = _set(source, True)
    assert preview["dry_run"] is True
    assert preview["source_revision"]["before_sha256"] == config_source_sha256(source)
    assert preview["current_enabled"] is False
    assert preview["target_enabled"] is True
    assert preview["preview_sha256"]
    assert preview["effect_scope"] == "assignment_scenario"
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
    assert "config build-bot" in result["rollback_hint"]
    assert set(result["verified_targets"]) == {
        str(source), str(tmp_path / "config.us.json"),
        str(tmp_path / "resolved" / "config.bot.json"),
    }
    assert yaml.safe_load(source.read_text())["portfolio"]["holdings"]["enabled"] is True
    assert yaml.safe_load(source.read_text())["portfolio"]["holdings"]["approved_non_futu_brokers"] == {"lx": ["银行"]}
    assert json.loads((tmp_path / "config.us.json").read_text())["portfolio"]["holdings"]["approved_non_futu_brokers"] == {"lx": ["银行"]}
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
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda _cfg: {"status": "ready_observed", "approved_non_futu_brokers": {"lx": []}})
    preview = _set(source, True)
    publish = inclusion.publish_yaml_config_generation

    def corrupt_bot_after_publish(**kwargs):
        result = publish(**kwargs)
        if kwargs["apply"]:
            Path(result["bot"]["output_config_path"]).write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(inclusion, "publish_yaml_config_generation", corrupt_bot_after_publish)
    with pytest.raises(AgentToolError) as caught:
        _set(source, True, apply=True, confirm=True,
             expected_source_sha256=preview["source_revision"]["before_sha256"],
             expected_preview_sha256=preview["preview_sha256"])
    error = caught.value
    assert error.code == "CONFIG_READBACK_FAILED"
    assert error.details["write_applied"] is True
    assert error.details["audit_id"]
    assert error.details["backup_path"]
    assert error.details["target"] == str(tmp_path / "resolved" / "config.bot.json")
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
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda _cfg: {"status": "ready_observed", "approved_non_futu_brokers": {"lx": []}})
    preview = _set(source, True)
    with pytest.raises(AgentToolError, match="preview"):
        _set(source, False, apply=True, confirm=True,
             expected_source_sha256=preview["source_revision"]["before_sha256"],
             expected_preview_sha256=preview["preview_sha256"])
    assert source.read_bytes() == before


def _scoped_probe_evidence(*, broker="Futu", classification="futu", count=1, status="complete"):
    return {
        "status": status, "warnings": [],
        "freshness": {"status": "fresh", "trust_status": "trusted", "observed_at_utc": "2026-09-29T00:00:00Z"},
        "scope": {
            "accounts": ["lx"], "holdings_scope": "non_futu",
            "broker_inventory": {"lx": {"brokers": [{"broker": broker, "classification": classification, "row_count": count}] if count else []}},
            "holding_counts": {"lx": {"source_rows": count, "included": count if classification == "non_futu" else 0,
                                      "excluded_futu": count if classification == "futu" else 0,
                                      "excluded_unknown_broker": count if classification == "unknown" else 0,
                                      "zero_quantity": 0, "unsupported": 0}},
        },
        "account_status": [{"account": "lx", "status": status}],
        "holdings": ([{"account": "lx", "broker": broker, "code": "BANK"}] if classification == "non_futu" else []),
    }


def test_holdings_probe_accepts_zero_eligible_and_displays_broker_inventory(monkeypatch) -> None:
    monkeypatch.setattr(inclusion, "read_portfolio_valuation_evidence", lambda **kwargs: _scoped_probe_evidence())
    result = inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})
    assert result["status"] == "ready_empty"
    assert result["eligible_rows"] == 0
    assert result["approved_non_futu_brokers"] == {"lx": []}
    assert result["broker_inventory"]["lx"][0]["broker"] == "Futu"

    monkeypatch.setattr(inclusion, "read_portfolio_valuation_evidence", lambda **kwargs: _scoped_probe_evidence(broker="银行", classification="non_futu"))
    result = inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})
    assert result["approved_non_futu_brokers"] == {"lx": ["银行"]}
    assert result["eligible_rows"] == 1


@pytest.mark.parametrize("classification,status", [("unknown", "complete"), ("non_futu", "partial")])
def test_holdings_probe_rejects_unknown_or_partial(monkeypatch, classification, status) -> None:
    monkeypatch.setattr(inclusion, "read_portfolio_valuation_evidence", lambda **kwargs: _scoped_probe_evidence(broker="manual" if classification == "unknown" else "银行", classification=classification, status=status))
    with pytest.raises(ValueError, match="incomplete or stale"):
        inclusion._probe_holdings({"portfolio_management": {"enabled": True}, "accounts": ["lx"]})


def test_holdings_confirmation_rejects_changed_broker_names_before_write(monkeypatch, tmp_path: Path) -> None:
    source = _source(tmp_path)
    names = ["银行"]
    monkeypatch.setattr(inclusion, "_probe_holdings", lambda _cfg: {
        "status": "ready_observed", "approved_non_futu_brokers": {"lx": list(names)},
    })
    preview = _set(source, True)
    names[:] = ["银行", "券商 B"]
    with pytest.raises(AgentToolError) as caught:
        _set(source, True, apply=True, confirm=True,
             expected_source_sha256=preview["source_revision"]["before_sha256"],
             expected_preview_sha256=preview["preview_sha256"])
    assert caught.value.code == "STALE_PREVIEW"
    assert not (tmp_path / "config.us.json").exists()
    assert "portfolio" not in yaml.safe_load(source.read_text())


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
    assert applied["effect_scope"] == "assignment_scenario"
    assert (tmp_path / "config.us.json").is_file()
