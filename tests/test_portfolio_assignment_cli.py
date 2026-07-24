from __future__ import annotations

import json

from src.interfaces.cli.main import parse_args
from src.interfaces.cli import portfolio_ops


def _result():
    return {
        "schema_version": "portfolio.assignment_scenario.v1",
        "status": "complete",
        "scope": {
            "accounts": ["lx", "sy"],
            "include_long_options": False,
        },
        "summary": {
            "assignment_count": 0,
            "short_put_count": 0,
            "short_call_count": 0,
        },
        "cash_coverage": {
            "available_cash_and_mmf_cny": "1000.00",
            "gross_put_requirement_cny": "0.00",
            "call_assignment_inflow_cny": "0.00",
            "ending_cash_net_estimated_cny": "1000.00",
            "terminal_funding_gap_cny": "0.00",
        },
        "distribution": {
            "gross_assets_cny": "1000.00",
            "liabilities_cny": "0.00",
            "net_assets_cny": "1000.00",
            "by_category": [
                {
                    "category": "cash",
                    "value_cny": "1000.00",
                    "weight_of_gross_assets": "1.000000",
                }
            ],
        },
        "warnings": [],
    }


def test_assignment_scenario_cli_parses_business_input_only():
    args = parse_args(
        [
            "portfolio",
            "assignment-scenario",
            "--accounts",
            "lx",
            "sy",
            "--format",
            "json",
        ]
    )

    assert args.command == "portfolio"
    assert args.portfolio_command == "assignment-scenario"
    assert args.accounts == ["lx", "sy"]
    assert args.format == "json"
    assert not hasattr(args, "price")
    assert not hasattr(args, "fx")
    assert not hasattr(args, "data_config")


def test_assignment_scenario_cli_json_and_text_share_application_result(
    monkeypatch,
    capsys,
):
    calls = []
    monkeypatch.setattr(
        portfolio_ops,
        "query_portfolio_assignment_scenario",
        lambda accounts: calls.append(list(accounts)) or _result(),
    )

    json_args = parse_args(
        ["portfolio", "assignment-scenario", "--accounts", "lx", "sy", "--format", "json"]
    )
    assert portfolio_ops.handle_portfolio_command(json_args) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == (
        "portfolio.assignment_scenario.v1"
    )

    text_args = parse_args(
        ["portfolio", "assignment-scenario", "--accounts", "lx", "sy"]
    )
    assert portfolio_ops.handle_portfolio_command(text_args) == 0
    rendered = capsys.readouterr().out
    assert "指派后资产分布（不含 Long Option）" in rendered
    assert "现金 + MMF：1000.00" in rendered
    assert calls == [["lx", "sy"], ["lx", "sy"]]
