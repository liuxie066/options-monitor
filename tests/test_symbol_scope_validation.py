"""Explicit empty scan scope must fail before configuration or execution."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import json
import socket

import pytest


EMPTY_SCOPES = ("", " \t\n ", ",", ",,", " , \t, ", "\u3000")


def _forbid(*_args, **_kwargs):
    pytest.fail("invalid symbol scope reached business I/O")


@pytest.fixture(autouse=True)
def _isolated_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(socket.socket, "connect", _forbid)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


def _config():
    return {
        "symbols": [
            {"symbol": "NVDA", "broker": "us", "sell_put": {"enabled": True}},
            {"symbol": "AMD", "broker": "us", "sell_put": {"enabled": True}},
        ],
        "templates": {},
        "runtime": {},
        "intake": {"symbol_aliases": {"CHIP": "NVDA"}},
    }


def _watchlist_kwargs(tmp_path, symbols, *, process_symbol_fn=_forbid, build_context_fn=_forbid):
    return {
        "py": "unused-python",
        "base": tmp_path,
        "cfg": _config(),
        "report_dir": tmp_path / "reports",
        "is_scheduled": False,
        "top_n": 3,
        "symbol_timeout_sec": 1,
        "portfolio_timeout_sec": 1,
        "want_scan": True,
        "no_context": True,
        "symbols_arg": symbols,
        "log": lambda _: None,
        "want_fn": lambda _: True,
        "apply_profiles_fn": lambda item, _profiles: dict(item),
        "process_symbol_fn": process_symbol_fn,
        "build_pipeline_context_fn": build_context_fn,
        "build_symbols_summary_fn": _forbid,
        "build_symbols_digest_fn": _forbid,
    }


@pytest.mark.parametrize("raw", EMPTY_SCOPES)
def test_account_scope_rejects_empty_outside_watchlist_fallback(raw, tmp_path):
    from src.application.account_config import build_account_runtime_config

    config = _config()
    original = deepcopy(config)
    with pytest.raises(ValueError, match="--symbols"):
        build_account_runtime_config(
            base_cfg=config, cfg_path=tmp_path / "unused.json",
            account="user1", markets_to_run=["us"], symbols_arg=raw,
        )
    assert config == original


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, ["NVDA", "AMD"]), ("nvda", ["NVDA"]), (" , CHIP,, ", ["NVDA"]),
     ("AMD,NVDA", ["NVDA", "AMD"])],
)
def test_account_scope_preserves_absence_aliases_and_config_order(raw, expected, tmp_path):
    from src.application.account_config import build_account_runtime_config

    result = build_account_runtime_config(
        base_cfg=_config(), cfg_path=tmp_path / "unused.json",
        account="user1", markets_to_run=["us"], symbols_arg=raw,
    )
    assert [item["symbol"] for item in result["symbols"]] == expected


@pytest.mark.parametrize("raw", EMPTY_SCOPES)
def test_watchlist_rejects_empty_before_context_or_processing(raw, tmp_path):
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    with pytest.raises(ValueError, match="--symbols"):
        run_watchlist_pipeline(**_watchlist_kwargs(tmp_path, raw))
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize("raw", EMPTY_SCOPES)
def test_default_watchlist_rejects_before_loading_frozen_batch(raw, monkeypatch, tmp_path):
    from src.application import pipeline_watchlist as mod

    monkeypatch.setattr(mod, "resolve_frozen_required_data_csv_bytes_batch", _forbid)
    with pytest.raises(ValueError, match="--symbols"):
        mod.run_watchlist_pipeline_default(
            py="unused-python", base=tmp_path, cfg=_config(),
            report_dir=tmp_path / "reports", state_dir=tmp_path / "state",
            shared_state_dir=None, required_data_dir=tmp_path / "required",
            is_scheduled=False, top_n=3, symbol_timeout_sec=1,
            portfolio_timeout_sec=1, want_scan=True, no_context=True,
            symbols_arg=raw, log=lambda _: None, want_fn=lambda _: True,
            source_account_run_id="synthetic-run",
            required_data_snapshot_manifest=tmp_path / "unused-manifest.json",
        )
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, ["NVDA", "AMD"]), ("nvda", ["NVDA"]), (" ,NVDA,, AMD, ", ["NVDA", "AMD"])],
)
def test_watchlist_keeps_valid_scope(raw, expected, tmp_path):
    from src.application.pipeline_watchlist import run_watchlist_pipeline

    seen = []

    def process(_py, _base, item, *_args, **_kwargs):
        seen.append(item["symbol"])
        return [{"symbol": item["symbol"], "strategy": "sell_put", "candidate_count": 0}]

    kwargs = _watchlist_kwargs(
        tmp_path, raw, process_symbol_fn=process,
        build_context_fn=lambda **_: ({}, None, None, None),
    )
    kwargs["build_symbols_summary_fn"] = lambda *_args, **_kwargs: None
    kwargs["build_symbols_digest_fn"] = lambda *_args, **_kwargs: None
    assert len(run_watchlist_pipeline(**kwargs)) == len(expected)
    assert seen == expected


@pytest.mark.parametrize("raw", EMPTY_SCOPES)
@pytest.mark.parametrize("surface", ("tick", "tick-cron", "scan-pipeline", "scan"))
def test_public_scan_commands_reject_empty_before_business_io(surface, raw, monkeypatch, tmp_path, capsys):
    from src.application import multi_account_tick, pipeline_runtime, tick_cron
    from src.interfaces.cli import main as cli

    monkeypatch.setattr(cli, "command_environment", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(multi_account_tick, "resolve_runtime_root", _forbid)
    monkeypatch.setattr(pipeline_runtime, "resolve_runtime_root", _forbid)
    monkeypatch.setattr(cli, "run_scan", _forbid)
    monkeypatch.setattr(
        cli, "run_tick_cron",
        lambda **kwargs: tick_cron.run_tick_cron(
            **kwargs, preflight_config_fn=_forbid, run_cmd=_forbid,
            environ={}, runtime_root=tmp_path,
        ),
    )
    missing_config = str(tmp_path / "not-read.json")
    if surface == "tick":
        argv = ["run", "tick", "--config", missing_config]
    elif surface == "tick-cron":
        argv = ["run", "tick-cron", "--market", "us", "--config", missing_config,
                "--lock-path", str(tmp_path / "not-created.lock")]
    elif surface == "scan-pipeline":
        argv = ["scan-pipeline", "--config", missing_config]
    else:
        argv = ["scan", "--config-path", missing_config]
    argv.extend(["--symbols", raw])
    if surface in {"tick", "scan-pipeline"}:
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
        assert "--symbols" in capsys.readouterr().err
    else:
        assert cli.main(argv) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "CONFIG_ERROR"
        assert "--symbols" in payload["error"]["message"]
    assert not (tmp_path / "not-created.lock").exists()
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("symbols", [None, []])
def test_cron_absent_scope_keeps_scheduled_plan(symbols):
    from src.application.tick_cron import build_tick_cron_plan

    plan = build_tick_cron_plan(market="us", symbols=symbols)
    assert plan.symbols == []
    assert plan.trigger_env["OM_TRIGGER_SOURCE"] == "cron"
    assert "--symbols" not in plan.tick_argv
    assert "--no-send" not in plan.tick_argv


def test_tick_multiple_argv_and_cron_valid_scope_keep_forwarding(monkeypatch, capsys):
    from src.application.tick_cron import build_tick_cron_plan
    from src.interfaces.cli import main as cli

    calls = []
    monkeypatch.setattr(cli, "command_environment", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(cli, "run_tick", lambda argv: calls.append(argv) or 0)
    assert cli.main(["run", "tick", "--config", "unused.json", "--symbols", "nvda", " AMD "]) == 0
    assert calls[0][calls[0].index("--symbols") + 1] == "nvda, AMD "
    plan = build_tick_cron_plan(market="us", symbols=["nvda", " AMD ", ",NVDA,"])
    assert plan.symbols == ["nvda", "AMD", ",NVDA,"]
    assert plan.tick_argv[plan.tick_argv.index("--symbols") + 1] == "nvda,AMD,,NVDA,"
    assert plan.trigger_env["OM_TRIGGER_SOURCE"] == "diagnostic"
    assert "--no-send" in plan.tick_argv


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), (" nvda, AMD,,nvda ", ["NVDA", "AMD", "NVDA"]),
     ("腾讯,POP", ["腾讯", "POP"]), ("NVDA AMD", ["NVDA AMD"])],
)
def test_operator_scan_preserves_existing_order_case_and_lexical_grammar(raw, expected, monkeypatch, capsys):
    from src.interfaces.cli import main as cli

    calls = []
    monkeypatch.setattr(cli, "command_environment", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(cli, "run_scan", lambda **kwargs: calls.append(kwargs) or {"ok": True})
    argv = ["scan"] + ([] if raw is None else ["--symbols", raw])
    assert cli.main(argv) == 0
    assert calls[0]["symbols"] == expected
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize("raw", EMPTY_SCOPES)
@pytest.mark.parametrize("as_list", [False, True])
def test_direct_tool_rejects_empty_before_config_or_output_resolution(raw, as_list, tmp_path):
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tools.materialization_impl import scan_opportunities_tool

    with pytest.raises(AgentToolError) as exc:
        scan_opportunities_tool(
            {"symbols": [raw] if as_list else raw},
            load_runtime_config=_forbid, resolve_data_config_ref=_forbid,
            resolve_output_root=_forbid, repo_base=_forbid, load_config=_forbid,
            run_watchlist_pipeline_default=_forbid, scan_summary_rows_fn=_forbid,
        )
    assert exc.value.code == "INPUT_ERROR"
    assert "--symbols" in exc.value.message
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({}, None), ({"symbols": None}, None), ({"symbols": []}, None),
     ({"symbols": ["AMD", " nvda ", ","]}, "AMD, nvda ,,"),
     ({"symbols": "NVDA AMD"}, "NVDA AMD")],
)
def test_direct_tool_preserves_absence_and_valid_forwarding(payload, expected, tmp_path):
    from src.application.agent_tools.materialization_impl import scan_opportunities_tool

    calls = []
    data, warnings, _meta = scan_opportunities_tool(
        payload, load_runtime_config=lambda **_: (tmp_path / "unused.json", {}),
        resolve_data_config_ref=_forbid, resolve_output_root=lambda _: tmp_path / "output",
        repo_base=lambda: tmp_path, load_config=lambda **_: {},
        run_watchlist_pipeline_default=lambda **kwargs: calls.append(kwargs) or [],
        scan_summary_rows_fn=lambda _: {"top_candidates": []},
    )
    assert calls[0]["symbols_arg"] == expected
    assert data["row_count"] == 0
    assert data["symbol_count"] == 0
    assert warnings == []


def test_wheel_policy_keeps_valid_empty_retained_scope(tmp_path):
    from src.application.account_config import build_account_runtime_config
    from src.application.config_sections import set_watchlist_config
    from src.application.opening_candidate_snapshot import strategy_policy_hash
    from src.application.tick_run_workspace import publish_account_run_config
    from src.application.wheel.candidate_snapshot import current_wheel_candidate_policy_hash

    config = _config()
    config_path = tmp_path / "synthetic-config.json"
    retained = build_account_runtime_config(
        base_cfg=config, cfg_path=config_path, account="user1", markets_to_run=[],
    )
    set_watchlist_config(retained, [])
    authority = publish_account_run_config(
        base=tmp_path, run_id="empty-scope", account="user1", config=retained,
    )
    retained_hash = strategy_policy_hash(retained)
    assert retained_hash != strategy_policy_hash(config)
    assert current_wheel_candidate_policy_hash(
        base=tmp_path, run_id="empty-scope", account="user1",
        config_path=config_path, config=config,
        snapshot={
            "account_config_sha256": authority.account_config_sha256,
            "strategy_policy_sha256": retained_hash,
        },
    ) == retained_hash
