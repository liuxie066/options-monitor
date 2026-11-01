from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.config_yaml_init import init_yaml_config
from src.interfaces.cli import main as cli
from src.interfaces.cli.journeys import manage_accounts, manage_symbols


REPO = Path(__file__).resolve().parents[1]


def _source(tmp_path, *, market="us", both=True):
    root = tmp_path / "isolated-runtime"
    symbol = "NVDA" if market == "us" else "0700.HK"
    policy = {"strategy": "both" if both else "csp", "csp_max_strike": 100, "csp_min_strike": 80}
    if both:
        policy.update(cc_min_strike=150, cc_max_strike=200)
    init_yaml_config(
        account_label="one", repo_root=REPO, output_config_yaml_path=root / "config.yaml",
        runtime_output_dir=root, markets=[market], **{f"{market}_symbols": [symbol]},
        futu_acc_id="12345", trd_env="SIMULATE", dry_run=False, symbol_policies={symbol: policy},
    )
    return root / "config.yaml"


def _snapshot(source):
    return {path.relative_to(source.parent): path.read_bytes()
            for path in source.parent.rglob("*") if path.is_file()}


def _inputs(source, values, prompts, *, before=None):
    iterator = iter(values)
    def answer(prompt):
        prompts.append(prompt)
        if before is not None:
            assert _snapshot(source) == before, "draft/preview wrote before confirmation"
        reply = next(iterator)
        if isinstance(reply, BaseException):
            raise reply
        return reply
    return answer


def test_symbol_edit_retries_current_field_and_keeps_accepted_six_field_draft(tmp_path, capsys):
    source = _source(tmp_path)
    before = _snapshot(source)
    prompts = []
    replies = [
        "edit", "US.NVDA", "maybe", "true",
        "0", "-1", "nan", "inf", "true", "90", "80", "110",
        "null", "true", "160", "140", "null", "y",
    ]
    assert manage_symbols(cli.main, source, input_fn=_inputs(source, replies, prompts, before=before)) == 0
    policy = yaml.safe_load(source.read_text())["markets"]["us"]["overrides"]["NVDA"]
    assert policy["sell_put"] == {"enabled": True, "min_strike": 90, "max_strike": 110}
    assert policy["covered_call"] == {"enabled": True, "min_strike": 160, "max_strike": None}
    assert sum(prompt.startswith("CSP enabled") for prompt in prompts) == 2
    assert sum(prompt.startswith("CSP min_strike") for prompt in prompts) == 6
    assert sum(prompt.startswith("CSP max_strike") for prompt in prompts) == 2
    assert sum(prompt.startswith("CC enabled") for prompt in prompts) == 2
    assert sum(prompt.startswith("CC min_strike") for prompt in prompts) == 1
    assert sum(prompt.startswith("CC max_strike") for prompt in prompts) == 2
    assert "请重新填写此项" in capsys.readouterr().out
    runtime = json.loads((source.parent / "config.us.json").read_text())["symbols"][0]
    assert runtime["sell_put"]["min_strike"] == 90
    assert runtime["sell_call"]["min_strike"] == 160
    assert runtime["sell_call"].get("max_strike") is None


def test_edit_enabling_cc_requires_its_min_at_that_field_not_at_enabled(tmp_path):
    source = _source(tmp_path, both=False)
    prompts = []
    replies = ["edit", "NVDA", "", "", "", "true", "", "-1", "150", "", "y"]
    assert manage_symbols(cli.main, source, input_fn=_inputs(source, replies, prompts)) == 0
    assert sum(prompt.startswith("CC enabled") for prompt in prompts) == 1
    assert sum(prompt.startswith("CC min_strike") for prompt in prompts) == 3
    policy = yaml.safe_load(source.read_text())["markets"]["us"]["overrides"]["NVDA"]
    assert policy["covered_call"]["enabled"] is True
    assert policy["covered_call"]["min_strike"] == 150


def test_edit_required_clear_retries_and_blank_keeps_optional_null_clears(tmp_path):
    source = _source(tmp_path)
    prompts = []
    replies = ["edit", "NVDA", "", "null", "null", "120", "", "", "null", "y"]
    assert manage_symbols(cli.main, source, input_fn=_inputs(source, replies, prompts)) == 0
    policy = yaml.safe_load(source.read_text())["markets"]["us"]["overrides"]["NVDA"]
    assert policy["sell_put"] == {"enabled": True, "min_strike": None, "max_strike": 120}
    assert policy["covered_call"] == {"enabled": True, "min_strike": 150, "max_strike": None}
    assert sum(prompt.startswith("CSP max_strike") for prompt in prompts) == 2


@pytest.mark.parametrize("raw", ["9988", "HK.09988"])
def test_add_hk_symbol_canonicalizes_before_policy_and_currency(tmp_path, capsys, raw):
    source = _source(tmp_path, market="hk")
    prompts = []
    replies = ["add", "NVDA", raw, "csp", "0", "300", "400", "200", "y"]
    assert manage_symbols(cli.main, source, input_fn=_inputs(source, replies, prompts)) == 0
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["hk"]["symbols"] == ["0700.HK", "9988.HK"]
    assert document["markets"]["hk"]["overrides"]["9988.HK"]["sell_put"]["min_strike"] == 200
    assert sum(prompt.startswith("标的代码") for prompt in prompts) == 2
    assert all("9988.HK" in prompt and "HKD" in prompt for prompt in prompts if "最高行权价" in prompt)
    output = capsys.readouterr().out
    assert "9988.HK 行权价单位：HKD 港元" in output
    assert "USD 美元" not in output


def test_new_market_accounts_retry_symbol_list_in_selected_market_keep_account_draft(tmp_path, capsys):
    source = _source(tmp_path)
    prompts = []
    before = _snapshot(source)
    replies = ["add", "hk", "second", "67890", "", "", "SIMULATE",
               "", "NVDA", "700, NVDA", "700, HK.00700, 9988",
               "csp", "300", "200", "cc", "500", "", "y"]
    assert manage_accounts(cli.main, source, input_fn=_inputs(source, replies, prompts, before=before)) == 0
    document = yaml.safe_load(source.read_text())
    assert document["markets"]["hk"]["symbols"] == ["0700.HK", "9988.HK"]
    assert document["markets"]["hk"]["accounts"] == ["second"]
    assert document["accounts"]["second"]["futu"]["trd_env"] == "SIMULATE"
    assert sum(prompt.startswith("新市场的监控标的") for prompt in prompts) == 4
    assert sum(prompt.startswith("账户标签") for prompt in prompts) == 1
    assert sum(prompt.startswith("富途账户 ID") for prompt in prompts) == 1
    assert "HKD 港元" in capsys.readouterr().out


@pytest.mark.parametrize("form", ["edit", "add", "account"])
@pytest.mark.parametrize("cancel", ["n", EOFError(), KeyboardInterrupt()])
def test_daily_forms_cancel_without_writes_even_after_retry(tmp_path, form, cancel):
    source = _source(tmp_path)
    before = _snapshot(source)
    prompts = []
    if form == "edit":
        replies = ["edit", "NVDA", "bad", "", "", "120", "", "", "", cancel]
        manage = manage_symbols
    elif form == "add":
        replies = ["add", "AAPL", "bad", "cc", "-1", "90", "", cancel]
        manage = manage_symbols
    else:
        replies = ["add", "hk", "second", "bad", "67890", "", "", "paper", "SIMULATE",
                   "NVDA", "700", "csp", "100", "", cancel]
        manage = manage_accounts
    action = lambda: manage(cli.main, source, input_fn=_inputs(source, replies, prompts, before=before))
    if isinstance(cancel, BaseException):
        with pytest.raises(type(cancel)):
            action()
    else:
        assert action() == 0
    assert _snapshot(source) == before


def test_all_edit_fields_blank_is_a_noop_without_confirmation(tmp_path):
    source = _source(tmp_path)
    before = _snapshot(source)
    prompts = []
    assert manage_symbols(cli.main, source, input_fn=_inputs(
        source, ["edit", "NVDA", "", "", "", "", "", ""], prompts,
    )) == 0
    assert not any("确认执行" in prompt for prompt in prompts)
    assert _snapshot(source) == before


@pytest.mark.parametrize("cancel", [EOFError(), KeyboardInterrupt()])
def test_edit_cancel_during_invalid_field_keeps_persisted_config(tmp_path, cancel):
    source = _source(tmp_path)
    before = _snapshot(source)
    prompts = []
    with pytest.raises(type(cancel)):
        manage_symbols(cli.main, source, input_fn=_inputs(
            source, ["edit", "NVDA", "true", "90", "80", cancel], prompts, before=before,
        ))
    assert sum(prompt.startswith("CSP enabled") for prompt in prompts) == 1
    assert sum(prompt.startswith("CSP min_strike") for prompt in prompts) == 1
    assert sum(prompt.startswith("CSP max_strike") for prompt in prompts) == 2
    assert not any("确认执行" in prompt for prompt in prompts)
    assert _snapshot(source) == before


def test_daily_account_fields_retry_only_invalid_field_and_preview_cancel(tmp_path):
    source = _source(tmp_path)
    before = _snapshot(source)
    prompts = []
    replies = ["add", "cn", "us", "bad label", "second", "abc", "67890",
               "bad host", "127.0.0.1", "0", "1.5", "11111", "paper", "simulate", "n"]
    assert manage_accounts(cli.main, source, input_fn=_inputs(source, replies, prompts, before=before)) == 0
    assert sum(prompt.startswith("账户标签") for prompt in prompts) == 2
    assert sum(prompt.startswith("富途账户 ID") for prompt in prompts) == 2
    assert sum(prompt.startswith("OpenD 地址") for prompt in prompts) == 2
    assert sum(prompt.startswith("OpenD 端口") for prompt in prompts) == 3
    assert sum(prompt.startswith("账户环境") for prompt in prompts) == 2
    assert _snapshot(source) == before


def test_edit_cross_bound_checks_effective_inherited_value(tmp_path):
    source = _source(tmp_path)
    document = yaml.safe_load(source.read_text())
    del document["markets"]["us"]["overrides"]["NVDA"]["sell_put"]["min_strike"]
    document["symbol_defaults"]["sell_put"] = {"min_strike": 95}
    source.write_text(yaml.safe_dump(document, sort_keys=False))
    prompts = []
    assert manage_symbols(cli.main, source, input_fn=_inputs(
        source, ["edit", "NVDA", "", "", "90", "105", "", "", "", "y"], prompts,
    )) == 0
    assert sum(prompt.startswith("CSP max_strike") for prompt in prompts) == 2
    written = yaml.safe_load(source.read_text())["markets"]["us"]["overrides"]["NVDA"]["sell_put"]
    assert "min_strike" not in written
    assert written["max_strike"] == 105
    runtime = json.loads((source.parent / "config.us.json").read_text())["symbols"][0]
    assert runtime["sell_put"]["min_strike"] == 95


def test_edit_can_disable_sides_then_clear_their_required_bounds(tmp_path):
    source = _source(tmp_path)
    prompts = []
    assert manage_symbols(cli.main, source, input_fn=_inputs(
        source, ["edit", "NVDA", "false", "null", "null", "false", "null", "null", "y"], prompts,
    )) == 0
    policy = yaml.safe_load(source.read_text())["markets"]["us"]["overrides"]["NVDA"]
    for key in ("sell_put", "covered_call"):
        assert policy[key] == {"enabled": False, "min_strike": None, "max_strike": None}


@pytest.mark.parametrize("side,low,high", [
    ("covered_call", 210, 250), ("covered_call", 110, 140),
    ("sell_put", 120, 140), ("sell_put", 60, 70),
])
def test_edit_moves_entire_strike_interval_in_one_confirmed_draft(tmp_path, side, low, high):
    source = _source(tmp_path)
    before = _snapshot(source)
    prompts = []
    csp = ["", str(low), str(high)] if side == "sell_put" else ["", "", ""]
    cc = ["", str(low), str(high)] if side == "covered_call" else ["", "", ""]
    assert manage_symbols(cli.main, source, input_fn=_inputs(
        source, ["edit", "NVDA", *csp, *cc, "y"], prompts, before=before,
    )) == 0
    policy = yaml.safe_load(source.read_text())["markets"]["us"]["overrides"]["NVDA"][side]
    assert policy["min_strike"] == low
    assert policy["max_strike"] == high
    assert sum("确认执行" in prompt for prompt in prompts) == 1


@pytest.mark.parametrize("cancel", ["n", EOFError(), KeyboardInterrupt()])
def test_shifted_cc_interval_draft_cancel_preserves_old_range(tmp_path, cancel):
    source = _source(tmp_path)
    before = _snapshot(source)
    prompts = []
    action = lambda: manage_symbols(cli.main, source, input_fn=_inputs(
        source, ["edit", "NVDA", "", "", "", "", "210", "200", "250", cancel], prompts, before=before,
    ))
    if isinstance(cancel, BaseException):
        with pytest.raises(type(cancel)):
            action()
    else:
        assert action() == 0
    assert sum(prompt.startswith("CC min_strike") for prompt in prompts) == 1
    assert sum(prompt.startswith("CC max_strike") for prompt in prompts) == 2
    assert _snapshot(source) == before
