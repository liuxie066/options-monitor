from __future__ import annotations

from typing import Any


METRIC_GLOSSARY_SCHEMA_VERSION = "om-metric-glossary-v1"


def metric_glossary_for_frame(frame: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(frame, dict):
        return {}
    return metric_glossary_for_namespace(str(frame.get("metric_namespace") or ""))


def metric_glossary_for_namespace(namespace: str) -> dict[str, Any]:
    name = str(namespace or "").strip()
    if name == "candidate_option_metrics":
        return {
            "schema_version": METRIC_GLOSSARY_SCHEMA_VERSION,
            "namespace": "candidate_option_metrics",
            "label": "candidate option metrics",
            "terms": {
                "net_income": {
                    "aliases": ["净收入", "net_income"],
                    "formula": "gross_income - futu_fee",
                    "components": {
                        "gross_income": "option_mid_price * contract_multiplier",
                        "futu_fee": "estimated Futu option fee for one sell contract",
                    },
                    "unit": "option contract currency",
                    "scope": "single candidate option contract evaluated by the scanner",
                    "source": "scan_sell_put / scan_sell_call candidate metrics",
                }
            },
        }
    if name == "account_income_metrics":
        return {
            "schema_version": METRIC_GLOSSARY_SCHEMA_VERSION,
            "namespace": "account_income_metrics",
            "label": "account income metrics",
            "terms": {
                "net_income_cny": {
                    "aliases": ["净收入", "net_income_cny"],
                    "formula": "income_cashflow_ex_assignment_stock converted to CNY",
                    "components": {
                        "premium_income_cny": "sell-open option premium converted to CNY",
                        "realized_pnl_cny": "realized option/assignment/stock sale PnL converted to CNY",
                        "other_net_income": "net_income_cny - premium_income_cny - realized_pnl_cny when residual exists",
                    },
                    "unit": "CNY",
                    "scope": "account/month income row in the OM local ledger",
                    "source": "monthly income analysis views",
                }
            },
        }
    return {}
