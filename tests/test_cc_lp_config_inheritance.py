from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.application import combo_yield_steps
from src.application.cc_lp_candidate_snapshot import (
    load_cc_lp_candidate_snapshot,
    project_cc_lp_candidates,
    seal_cc_lp_candidate_snapshot,
)
from src.application.cc_lp_steps import run_cc_lp_scan
from src.application.combo_yield_config import derive_combo_yield_policy
from src.application.config_profiles import apply_profiles
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config
from test_cc_lp_candidate_snapshot import _dependencies, _scope
from test_cc_lp_steps import (
    DECISION_NOW,
    _available_cc_lp_context,
    _converter,
    _put_row,
    _scan_call_row,
)
from test_config_yaml import _FUTU_ACCOUNTS_YAML


REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_symbol(tmp_path, *, templates, use, override=None):
    document = yaml.safe_load(_FUTU_ACCOUNTS_YAML)
    local = {
        "sell_put": False,
        "covered_call": {"enabled": True, "strategy": "insurance_underwriting"},
        "combo_yield": {"enabled": True, "variant": "cc_lp", **(override or {})},
    }
    if use:
        local["use"] = use
    document.update({
        "templates": templates,
        "markets": {"us": {
            "accounts": ["lx"], "symbols": ["NVDA"],
            "overrides": {"NVDA": local},
        }},
    })
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    cfg, _ = resolve_yaml_runtime_config(
        repo_root=REPO_ROOT, market="us", config_path=config_path,
    )
    before = deepcopy(cfg)
    resolved = resolve_watchlist_item_runtime_config(
        item=cfg["symbols"][0], profiles=cfg["templates"], apply_profiles_fn=apply_profiles,
    )
    assert cfg == before
    return resolved


def _facade_scan(tmp_path, monkeypatch, symbol_cfg, *, puts=None):
    evidence = []
    calls = []
    variant = combo_yield_steps.run_cc_lp_variant

    def funding_call(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame([_scan_call_row()])

    def scanner(**kwargs):
        return run_cc_lp_scan(
            **kwargs, run_sell_call_scan_fn=funding_call,
            now_utc_fn=lambda: DECISION_NOW,
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            combo_yield_steps, "run_cc_lp_variant",
            lambda **kwargs: variant(**kwargs, run_cc_lp_scan_fn=scanner),
        )
        summary = combo_yield_steps.run_combo_yield_for_symbol_and_summarize(
            sym="NVDA", symbol="NVDA", symbol_lower="nvda", symbol_cfg=symbol_cfg,
            sell_put_cfg=symbol_cfg["sell_put"], top_n=5,
            required_data_dir=tmp_path, report_dir=tmp_path / "reports", is_scheduled=False,
            exchange_rate_converter=_converter(), portfolio_ctx=_available_cc_lp_context(),
            stock={"shares": 100, "can_sell_qty": 100, "avg_cost": 90.0},
            required_data_frame=pd.DataFrame(puts or [_put_row()]),
            combo_evidence_sink_fn=evidence.append,
        )
    assert len(calls) == 1
    assert len(evidence) == 1
    assert summary["candidate_count"] == len(evidence[0]["ranked_pairs"])
    return summary, evidence[0]["ranked_pairs"]


def _roundtrip(tmp_path, pairs):
    sealed = seal_cc_lp_candidate_snapshot(
        base=tmp_path, run_id="inheritance-run", account="lx", market="us",
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(), scan_statuses=[_scope()], ranked_pairs=pairs,
        run_mode={"scan_mode": "standard", "executable": True}, sealed_at=DECISION_NOW,
    )
    loaded = load_cc_lp_candidate_snapshot(base=tmp_path, run_id="inheritance-run", account="lx")
    assert loaded == sealed
    return project_cc_lp_candidates(loaded)


@pytest.mark.parametrize(
    "use,override,expected_retention,expected_explicit,expected_count",
    [
        ("strict", {}, .90, True, 0),
        (["strict", "loose"], {}, .30, True, 1),
        (["loose", "strict"], {}, .90, True, 0),
        ("strict", {"min_net_credit_retention": .30}, .30, True, 1),
        ("strict", {"min_net_credit_retention": .60}, .60, True, 0),
        ("strict", {"min_net_credit_retention": 0.0}, 0.0, True, 1),
        ("strict", {"min_net_credit_retention": None}, None, True, 1),
        (None, {}, .60, False, 1),
    ],
)
def test_yaml_cc_lp_retention_survives_profiles_and_sealed_scan(
    tmp_path, monkeypatch, use, override, expected_retention, expected_explicit, expected_count,
):
    resolved = _resolve_symbol(
        tmp_path,
        templates={
            "strict": {"combo_yield": {"min_net_credit_retention": .90}},
            "loose": {"combo_yield": {"min_net_credit_retention": .30}},
        },
        use=use, override=override,
    )
    policy = derive_combo_yield_policy(resolved["combo_yield"], market="us")
    summary, pairs = _facade_scan(tmp_path, monkeypatch, resolved)
    persisted = _roundtrip(tmp_path, pairs)
    assert summary["status"] == ("candidates_found" if expected_count else "no_candidate")
    assert len(persisted) == expected_count
    assert policy.config["min_net_credit_retention"] == expected_retention
    assert ("min_net_credit_retention" in policy.explicit_fields) is expected_explicit


def test_yaml_cc_lp_inherits_each_authored_leg_field(tmp_path):
    resolved = _resolve_symbol(
        tmp_path,
        templates={"paired": {"combo_yield": {
            "call": {"min_delta": .20, "max_delta": .30},
            "put": {"min_delta": .10, "max_delta": .25},
        }}},
        use="paired", override={"call": {"max_delta": .28}},
    )
    policy = derive_combo_yield_policy(resolved["combo_yield"], market="us")
    assert policy.config["call"] == {"min_delta": .20, "max_delta": .28}
    assert policy.config["put"] == {"min_delta": .10, "max_delta": .25}
    assert set(policy.explicit_fields) == {"enabled", "variant", "call", "put"}


def test_cc_lp_zero_put_spread_stays_first_in_facade_and_snapshot(tmp_path, monkeypatch):
    resolved = _resolve_symbol(tmp_path, templates={}, use=None)
    summary, pairs = _facade_scan(
        tmp_path, monkeypatch, resolved,
        puts=[
            _put_row(contract_symbol="NVDA260828P90000", bid=2.2, ask=2.2),
            _put_row(contract_symbol="NVDA260828P90001", bid=2.0, ask=2.2),
        ],
    )
    persisted = _roundtrip(tmp_path, pairs)
    assert summary["status"] == "candidates_found"
    assert summary["candidate_count"] == 2
    assert persisted[0]["net_credit_retention"] == persisted[1]["net_credit_retention"]
    assert persisted[0]["put_delta"] == persisted[1]["put_delta"]
    assert persisted[0]["put_spread_ratio"] == 0.0
    assert persisted[1]["put_spread_ratio"] > 0.0
    assert [row["put_contract_symbol"] for row in pairs] == [
        "NVDA260828P90000", "NVDA260828P90001",
    ]
    assert [row["put_contract_symbol"] for row in persisted] == [
        "NVDA260828P90000", "NVDA260828P90001",
    ]
