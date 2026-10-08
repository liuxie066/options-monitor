from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml_accounts import list_yaml_accounts, mutate_yaml_account_config
from src.application.config_yaml_init import init_yaml_config
from src.application.config_yaml_symbols import mutate_yaml_symbol_config, set_yaml_symbol_config
from src.interfaces.cli.account_ops import handle_account_command
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.setup_ops import run_setup_init
from src.interfaces.cli.symbols import main as symbols_main

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("market,symbol,added", [("us", "NVDA", "AAPL"), ("hk", "0700.HK", "0005.HK")])
@pytest.mark.parametrize("environment", ["REAL", "SIMULATE"])
def test_setup_selected_endpoint_reaches_quote_route_and_future_symbols(tmp_path, market, symbol, added, environment):
    from src.application.account_config import resolve_account_futu_settings
    from src.application.futu_quote_routing import resolve_futu_quote_route

    root = tmp_path / "runtime"
    args = parse_args(["setup", "init", "--account-label", "lx", "--output-dir", str(root), "--market", market,
                       f"--{market}-symbol", symbol, "--symbol-strategy", f"{symbol}=csp",
                       "--csp-max-strike", f"{symbol}=100", "--futu-acc-id", "12345",
                       "--futu-host", "127.0.0.2", "--futu-port", "22222", "--trd-env", environment, "--apply"])
    _, applied = run_setup_init(args, repo_base_fn=lambda: REPO, input_is_tty=lambda: False,
                                user_home=tmp_path / "home")
    assert applied
    source = root / "config.yaml"
    assert symbols_main(["add", added, "--market", market, "--strategy", "csp", "--csp-max-strike", "100",
                         "--config-yaml", str(source), "--apply"]) == 0
    runtime = json.loads((root / f"config.{market}.json").read_text())
    account = resolve_account_futu_settings(runtime, account="lx")
    route = resolve_futu_quote_route(runtime)
    assert (account["host"], account["port"], account["trd_env"]) == ("127.0.0.2", 22222, environment)
    assert (route.host, route.port) == ("127.0.0.2", 22222)
    assert len(route.members) == 2
    assert all((item["fetch"]["host"], item["fetch"]["port"]) == ("127.0.0.2", 22222) for item in runtime["symbols"])


@pytest.mark.parametrize("raw,canonical", [("700", "0700.HK"), ("HK.00700", "0700.HK"), ("POP", "9992.HK")])
def test_setup_alias_policy_remains_manageable_through_daily_symbol_commands(tmp_path, raw, canonical):
    root = tmp_path / "runtime"
    args = parse_args(["setup", "init", "--account-label", "lx", "--output-dir", str(root), "--market", "hk",
                       "--hk-symbol", raw, "--hk-symbol", "0005.HK",
                       "--symbol-strategy", f"{canonical}=csp", "--csp-max-strike", f"{raw}=100",
                       "--symbol-strategy", "0005.HK=cc", "--cc-min-strike", "0005.HK=50",
                       "--futu-acc-id", "12345", "--trd-env", "REAL", "--apply"])
    _, applied = run_setup_init(args, repo_base_fn=lambda: REPO, input_is_tty=lambda: False,
                                user_home=tmp_path / "home")
    assert applied
    source = root / "config.yaml"
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["hk"]["symbols"] == [canonical, "0005.HK"]
    assert document["markets"]["hk"]["overrides"][canonical]["sell_put"]["max_strike"] == 100
    scope = ["--market", "hk", "--config-yaml", str(source)]
    assert symbols_main(["edit", raw, "--set", "sell_put.max_strike=90", *scope, "--apply"]) == 0
    with pytest.raises(SystemExit, match="already exists"):
        symbols_main(["add", canonical, "--strategy", "csp", "--csp-max-strike", "100", *scope, "--apply"])
    assert symbols_main(["rm", raw, *scope, "--apply"]) == 0
    assert yaml.safe_load(source.read_text())["markets"]["hk"]["symbols"] == ["0005.HK"]


@pytest.mark.parametrize("symbols,policies,error", [
    (["NVDA"], {"NVDA": {"strategy": "csp", "csp_max_strike": 100}}, "belongs to US, not hk"),
    (["700"], {"700": {"strategy": "csp", "csp_max_strike": 100},
               "0700.HK": {"strategy": "cc", "cc_min_strike": 200}}, "duplicate symbol policy"),
])
def test_starter_rejects_wrong_market_or_conflicting_alias_policies_before_write(tmp_path, symbols, policies, error):
    with pytest.raises(AgentToolError, match=error):
        init_yaml_config(account_label="lx", repo_root=REPO, output_config_yaml_path=tmp_path / "config.yaml", markets=["hk"],
                         hk_symbols=symbols, symbol_policies=policies, futu_acc_id="12345")
    assert not list(tmp_path.iterdir())


def test_new_market_account_canonicalizes_initial_symbols_and_policies(tmp_path):
    from src.application.futu_quote_routing import resolve_futu_quote_route

    source = _source(tmp_path)
    result = mutate_yaml_account_config(repo_root=REPO, config_path=source, action="add", market="hk",
                                        account_label="lx", symbols=["700", "HK.00700"],
                                        symbol_policies={"700": {"strategy": "cc", "cc_min_strike": 400}}, apply=True)
    assert result["write_applied"] is True
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["hk"]["symbols"] == ["0700.HK"]
    assert document["markets"]["hk"]["overrides"]["0700.HK"]["covered_call"]["min_strike"] == 400
    route = resolve_futu_quote_route(json.loads((tmp_path / "config.hk.json").read_text()))
    assert (route.host, route.port) == ("127.0.0.2", 11112)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "config.yaml"
    init_yaml_config(account_label="lx", repo_root=REPO, output_config_yaml_path=source, runtime_output_dir=tmp_path,
                     markets=["us"], futu_acc_id="12345", futu_host="127.0.0.2", futu_port=11112,
                     trd_env="SIMULATE", us_symbols=["NVDA"],
                     symbol_policies={"NVDA": {"strategy": "both", "csp_max_strike": 100, "cc_min_strike": 150}})
    return source


def _bytes(tmp_path: Path) -> dict[str, bytes]:
    return {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file() and path.name != "config_authoring.lock"}


def test_account_list_reports_selected_instance_environment_and_endpoint(tmp_path: Path) -> None:
    source = _source(tmp_path)
    before = _bytes(tmp_path)
    result = handle_account_command(parse_args(["accounts", "list", "--config-yaml", str(source)]))
    assert result["data"]["accounts"] == [{"market": "us", "account_label": "lx", "futu_acc_id": "12345",
                                            "futu_host": "127.0.0.2", "futu_port": 11112,
                                            "trd_env": "SIMULATE", "symbols": ["NVDA"]}]
    assert _bytes(tmp_path) == before


def test_second_account_shares_market_symbols_and_readback_environment(tmp_path: Path) -> None:
    source = _source(tmp_path)
    before_symbols = yaml.safe_load(source.read_text())["markets"]["us"]["overrides"]
    args = ["accounts", "add", "--market", "us", "--account-label", "second", "--futu-acc-id", "67890",
            "--trd-env", "REAL", "--futu-host", "127.0.0.3", "--futu-port", "22222", "--config-yaml", str(source)]
    preview = handle_account_command(parse_args(args))["data"]
    assert preview["summary"]["shared_symbols"] == ["NVDA"]
    assert preview["write_applied"] is False
    result = handle_account_command(parse_args([*args, "--apply", "--confirm", "--expected-source-sha256",
                                                preview["source_revision"]["before_sha256"]]))["data"]
    assert result["write_applied"] is True
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["us"]["overrides"] == before_symbols
    assert document["markets"]["us"]["accounts"] == ["lx", "second"]
    runtime = json.loads((tmp_path / "config.us.json").read_text())
    assert runtime["account_settings"]["second"]["futu"] == {
        "account_id": "67890", "host": "127.0.0.3", "port": 22222, "trd_env": "REAL"}
    assert len(runtime["symbols"]) == 1


def test_new_market_requires_user_policies_then_publishes_one_generation(tmp_path: Path) -> None:
    source = _source(tmp_path)
    command = ["accounts", "add", "--market", "hk", "--account-label", "second", "--futu-acc-id", "67890",
               "--trd-env", "REAL", "--config-yaml", str(source)]
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError, match="hk symbols are required"):
        handle_account_command(parse_args([*command, "--apply", "--confirm"]))
    with pytest.raises(AgentToolError, match="cc-min-strike"):
        handle_account_command(parse_args([*command, "--symbol", "0700.HK", "--symbol-strategy", "0700.HK=cc",
                                           "--apply", "--confirm"]))
    assert _bytes(tmp_path) == before
    result = handle_account_command(parse_args([*command, "--symbol", "0700.HK", "--symbol-strategy", "0700.HK=cc",
                                                "--cc-min-strike", "0700.HK=400", "--apply", "--confirm"]))["data"]
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["us"]["symbols"] == ["NVDA"]
    assert document["markets"]["hk"]["symbols"] == ["0700.HK"]
    for market in ("us", "hk"):
        runtime = json.loads((tmp_path / f"config.{market}.json").read_text())
        assert runtime["_resolved"]["config_yaml_sha256"] == result["source_revision"]["after_sha256"]
    hk = json.loads((tmp_path / "config.hk.json").read_text())
    assert hk["symbols"][0]["sell_call"]["min_strike"] == 400
    assert hk["symbols"][0]["sell_put"]["enabled"] is False


def test_existing_account_explicit_market_link_preserves_identity(tmp_path: Path) -> None:
    source = _source(tmp_path)
    before_account = yaml.safe_load(source.read_text())["accounts"]["lx"]
    args = dict(repo_root=REPO, action="add", market="hk", account_label="lx", config_path=source,
                symbols=["0700.HK"], symbol_policies={"0700.HK": {"strategy": "cc", "cc_min_strike": 400}})
    with pytest.raises(AgentToolError, match="account_type must be one of"):
        mutate_yaml_account_config(**args, account_type="external_holdings", apply=True)
    with pytest.raises(AgentToolError, match="preserve its mapping"):
        mutate_yaml_account_config(**args, trd_env="REAL", apply=True)
    assert "hk" not in yaml.safe_load(source.read_text())["markets"]
    mutate_yaml_account_config(**args, apply=True)
    document = yaml.safe_load(source.read_text())
    assert document["accounts"]["lx"] == before_account
    assert document["markets"]["hk"]["accounts"] == ["lx"]
    assert {row["market"] for row in list_yaml_accounts(repo_root=REPO, config_path=source)["accounts"]} == {"us", "hk"}


@pytest.mark.parametrize("field,value", [("trd_env", "PAPER"), ("futu_port", 0), ("futu_port", 1.5),
                                         ("futu_port", 65536), ("futu_host", "bad host"), ("futu_acc_id", "oops")])
def test_invalid_account_edit_has_no_effect(tmp_path: Path, field: str, value: object) -> None:
    source = _source(tmp_path)
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError):
        mutate_yaml_account_config(repo_root=REPO, action="edit", market="us", account_label="lx",
                                   config_path=source, apply=True, **{field: value})
    assert _bytes(tmp_path) == before


def test_account_edit_rejects_stale_preview_and_preserves_omitted_fields(tmp_path: Path) -> None:
    source = _source(tmp_path)
    options = dict(repo_root=REPO, action="edit", market="us", account_label="lx", config_path=source, futu_port=22222)
    preview = mutate_yaml_account_config(**options)
    source.write_text(source.read_text() + "\n# concurrently edited\n")
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        mutate_yaml_account_config(**options, apply=True, expected_source_sha256=preview["source_revision"]["before_sha256"])
    assert _bytes(tmp_path) == before
    mutate_yaml_account_config(**options, apply=True)
    result = list_yaml_accounts(repo_root=REPO, config_path=source)["accounts"][0]
    assert (result["futu_acc_id"], result["trd_env"], result["futu_host"], result["futu_port"]) == (
        "12345", "SIMULATE", "127.0.0.2", 22222)


@pytest.mark.parametrize("path,value", [("sell_put.max_strike", None), ("sell_call.min_strike", None),
                                        ("sell_put.max_strike", 0), ("sell_call.min_strike", -1),
                                        ("sell_put.max_strike", float("inf")), ("sell_call.min_strike", float("nan")),
                                        ("sell_put.min_strike", 110), ("sell_call.max_strike", 140)])
def test_symbol_edit_rejects_invalid_effective_bound_without_writes(tmp_path: Path, path: str, value: object) -> None:
    source = _source(tmp_path)
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError):
        mutate_yaml_symbol_config(repo_root=REPO, market="us", config_path=source, apply=True,
                                  payload={"action": "edit", "symbol": "NVDA", "set": {path: value}})
    assert _bytes(tmp_path) == before


def test_symbol_edit_preserves_other_policy_and_disabling_needs_no_bound(tmp_path: Path) -> None:
    source = _source(tmp_path)
    options = dict(repo_root=REPO, market="us", config_path=source)
    result = mutate_yaml_symbol_config(**options, payload={"action": "edit", "symbol": "NVDA",
                                                           "set": {"sell_put.max_strike": 90}}, apply=True)
    assert result["summary"]["before_effective"]["sell_put"]["max_strike"] == 100
    assert result["summary"]["after_effective"]["sell_call"]["min_strike"] == 150
    assert result["summary"]["affected_accounts"] == ["lx"]
    mutate_yaml_symbol_config(**options, payload={"action": "edit", "symbol": "NVDA",
                                                  "set": {"sell_put.enabled": False, "sell_put.max_strike": None}}, apply=True)
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError, match="requires max_strike"):
        set_yaml_symbol_config(**options, sell_put_enabled=True, symbol="NVDA", apply=True)
    assert _bytes(tmp_path) == before
    set_yaml_symbol_config(**options, sell_put_enabled=True, sell_put_max_strike=80, symbol="NVDA", apply=True)


def test_symbol_edit_honors_inherited_template_bounds_and_leaves_unrelated_legacy_target(tmp_path: Path) -> None:
    source = _source(tmp_path)
    document = yaml.safe_load(source.read_text())
    document["markets"]["us"]["symbols"].append("AAPL")  # existing unconstrained policy is not the mutation target
    document["templates"] = {"put_base": {"sell_put": {"max_strike": 100}}}
    del document["markets"]["us"]["overrides"]["NVDA"]["sell_put"]["max_strike"]
    source.write_text(yaml.safe_dump(document))
    result = mutate_yaml_symbol_config(repo_root=REPO, market="us", config_path=source, apply=True,
                                       payload={"action": "edit", "symbol": "NVDA", "set": {"sell_call.min_strike": 160}})
    assert result["summary"]["after_effective"]["sell_put"]["max_strike"] == 100
    assert yaml.safe_load(source.read_text())["markets"]["us"]["symbols"] == ["NVDA", "AAPL"]


def test_symbols_and_compatibility_facade_reject_stale_source(tmp_path: Path, capsys) -> None:
    source = _source(tmp_path)
    args = ["edit", "NVDA", "--set", "sell_put.max_strike=90", "--config-yaml", str(source), "--format", "json"]
    symbols_main(args)
    preview = json.loads(capsys.readouterr().out)
    revision = preview["source_revision"]["before_sha256"]
    source.write_text(source.read_text() + "\n# external edit\n")
    before = _bytes(tmp_path)
    with pytest.raises(SystemExit, match="STALE_PREVIEW"):
        symbols_main([*args, "--apply", "--expected-source-sha256", revision])
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        set_yaml_symbol_config(repo_root=REPO, market="us", symbol="NVDA", config_path=source,
                               sell_put_max_strike=90, apply=True, expected_source_sha256=revision)
    assert _bytes(tmp_path) == before


@pytest.mark.parametrize("environment", ["REAL", "SIMULATE"])
def test_interactive_setup_default_directory_explicit_identity_and_optouts(tmp_path: Path, monkeypatch, environment: str) -> None:
    from src.interfaces.cli import setup_ops
    target = tmp_path / "default-runtime"
    monkeypatch.setattr(setup_ops, "_default_setup_dir", lambda: target)
    prompts = []
    answers = iter(["us", "127.0.0.2", "11112", environment, "mine", "12345", "NVDA", "csp", "100", "", "yes"])

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    output, applied = run_setup_init(parse_args(["setup", "init"]), repo_base_fn=lambda: REPO,
                                     user_home=tmp_path / "home", input_is_tty=lambda: True, input_fn=answer)
    assert applied and "已写入并回读" in output
    assert not any(prompt.startswith("配置目录") for prompt in prompts)
    document = yaml.safe_load((target / "config.yaml").read_text())
    assert document["notifications"]["enabled"] is False
    assert document["bot"]["enabled"] is False
    assert document["bot"]["enabled"] is False
    assert list(document["markets"]) == ["us"]
    assert document["accounts"]["mine"]["futu"]["trd_env"] == environment
    generated = json.loads((target / "config.us.json").read_text())
    assert generated["account_settings"]["mine"]["futu"]["trd_env"] == environment


@pytest.mark.parametrize("answers,error", [(["us", "", "", "", "mine", "12345"], "requires REAL or SIMULATE"),
                                          (["us", "", "", "REAL", "mine", ""], "requires a Futu account ID")])
def test_interactive_setup_does_not_accept_missing_identity(tmp_path: Path, answers: list[str], error: str) -> None:
    target = tmp_path / "runtime"
    values = iter(answers)
    with pytest.raises(AgentToolError, match=error):
        run_setup_init(parse_args(["setup", "init", "--output-dir", str(target)]), repo_base_fn=lambda: REPO,
                       user_home=tmp_path / "home", input_is_tty=lambda: True, input_fn=lambda _: next(values))
    assert not target.exists()


@pytest.mark.parametrize("cancel_at", range(11))
def test_first_install_cancel_before_confirmation_never_publishes(tmp_path: Path, cancel_at: int) -> None:
    target = tmp_path / "runtime"
    answers = ["us", "", "", "REAL", "lx", "12345", "NVDA", "csp", "100", "", "yes"]
    index = 0

    def answer(_prompt: str) -> str:
        nonlocal index
        if index == cancel_at:
            raise KeyboardInterrupt
        value = answers[index]
        index += 1
        return value

    args = parse_args(["setup", "init", "--output-dir", str(target)])
    if cancel_at == 10:
        output, applied = run_setup_init(args, repo_base_fn=lambda: REPO, user_home=tmp_path / "home",
                                         input_is_tty=lambda: True, input_fn=answer)
        assert not applied and "已取消" in output
    else:
        with pytest.raises(AgentToolError, match="cancelled"):
            run_setup_init(args, repo_base_fn=lambda: REPO, user_home=tmp_path / "home",
                           input_is_tty=lambda: True, input_fn=answer)
    assert not target.exists()
    assert not (tmp_path / "home").exists()


def test_new_account_cli_requires_explicit_environment(tmp_path: Path) -> None:
    source = _source(tmp_path)
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError, match="requires --trd-env"):
        handle_account_command(parse_args(["accounts", "add", "--account-label", "second", "--market", "us",
                                           "--futu-acc-id", "67890", "--config-yaml", str(source), "--apply", "--confirm"]))
    assert _bytes(tmp_path) == before


def test_account_policy_for_unselected_symbol_is_not_ignored(tmp_path: Path) -> None:
    source = _source(tmp_path)
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError, match="unselected symbol"):
        handle_account_command(parse_args(["accounts", "add", "--account-label", "second", "--market", "us",
                                           "--futu-acc-id", "67890", "--trd-env", "REAL", "--config-yaml", str(source),
                                           "--symbol-strategy", "TSLA=csp", "--apply", "--confirm"]))
    assert _bytes(tmp_path) == before


def test_symbol_owner_rejects_cross_market_input_before_writes(tmp_path: Path) -> None:
    source = _source(tmp_path)
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError, match="belongs to HK, not us"):
        set_yaml_symbol_config(repo_root=REPO, market="us", symbol="0700.HK", config_path=source,
                               covered_call_min_strike=400, apply=True)
    assert _bytes(tmp_path) == before


def test_unmodified_legacy_strategy_is_preserved_while_changed_side_is_validated(tmp_path: Path) -> None:
    source = _source(tmp_path)
    document = yaml.safe_load(source.read_text())
    del document["markets"]["us"]["overrides"]["NVDA"]["sell_put"]["max_strike"]
    source.write_text(yaml.safe_dump(document))
    options = dict(repo_root=REPO, market="us", config_path=source)
    mutate_yaml_symbol_config(**options, payload={"action": "edit", "symbol": "NVDA",
                                                  "set": {"sell_call.min_strike": 160}}, apply=True)
    before = _bytes(tmp_path)
    with pytest.raises(AgentToolError, match="requires max_strike"):
        mutate_yaml_symbol_config(**options, payload={"action": "edit", "symbol": "NVDA",
                                                      "set": {"sell_put.min_strike": 50}}, apply=True)
    assert _bytes(tmp_path) == before
    mutate_yaml_symbol_config(**options, payload={"action": "edit", "symbol": "NVDA",
                                                  "set": {"sell_put.max_strike": 100}}, apply=True)
