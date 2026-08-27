from __future__ import annotations

import re
from typing import Any, Mapping

from src.application.account_config import (
    account_settings_from_config,
    resolve_account_futu_settings,
)
from src.infrastructure.futu_gateway import build_futu_gateway


EXPERIENCE_BANNER = "体验模式｜演示账户假设｜未读取账户现金与持仓｜不可作为可执行建议"
EXPERIENCE_FIELDS = {
    "scan_mode": "experience",
    "capacity_source": "demo_scenario",
    "executable": False,
}


def experience_fields(account_display_name: str) -> dict[str, Any]:
    name = str(account_display_name or "").strip()
    if not name:
        raise ValueError("experience account display name is required")
    return {**EXPERIENCE_FIELDS, "account_display_name": name}


def validate_experience_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = experience_fields(str(payload.get("account_display_name") or ""))
    if any(payload.get(key) != value for key, value in fields.items()):
        raise ValueError("experience result contract is invalid")
    return fields


def is_experience_payload(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("scan_mode") or "").strip().lower() == "experience"


def validate_experience_request(
    *,
    config: Mapping[str, Any],
    accounts: list[str],
    no_send: bool,
    smoke: bool,
    trigger_context: Mapping[str, Any],
    opend_phone_verify_continue: bool,
) -> None:
    if not no_send:
        raise ValueError("experience mode requires --no-send")
    if smoke:
        raise ValueError("experience mode cannot be combined with --smoke")
    if opend_phone_verify_continue:
        raise ValueError(
            "experience mode cannot clear OpenD phone-verification state"
        )
    source = str(trigger_context.get("source") or "").strip().lower()
    if source not in {"", "manual"}:
        raise ValueError("experience mode is available only from a direct manual run")
    for account in accounts:
        settings = resolve_account_futu_settings(config, account=account)
        if str(settings.get("trd_env") or "").strip().upper() != "SIMULATE":
            raise ValueError(
                "every selected account must explicitly use trd_env=SIMULATE"
            )


def resolve_experience_account_display_name(
    *,
    config: Mapping[str, Any],
    account: str,
) -> str:
    settings = resolve_account_futu_settings(config, account=account)
    account_id = str(settings.get("account_id") or "").strip()
    if not account_id:
        raise ValueError("configured simulated account identity is unavailable")
    account_settings = account_settings_from_config(dict(config)).get(
        str(account or "").strip().lower(),
        {},
    )
    generated = config.get("_generated")
    market = str(
        account_settings.get("market")
        or (generated.get("market") if isinstance(generated, Mapping) else "")
        or config.get("market")
        or ""
    ).strip().upper()
    if market not in {"US", "HK"}:
        raise ValueError("simulated account market metadata is unavailable")
    gateway = build_futu_gateway(
        host=str(settings.get("host") or "127.0.0.1"),
        port=int(settings.get("port") or 11111),
        is_option_chain_cache_enabled=False,
    )
    try:
        metadata = gateway.get_account_metadata(
            expected_account_id=account_id,
            trd_env="SIMULATE",
            expected_market=market,
        )
    finally:
        gateway.close()
    if not metadata.get("matched"):
        raise ValueError("configured simulated account metadata is unavailable")

    market_name = "美股" if market == "US" else "港股"
    sim_type = str(metadata.get("sim_acc_type") or "").strip().upper()
    if "OPTION" in sim_type:
        type_name = "模拟期权账户"
    elif "STOCK" in sim_type:
        type_name = "模拟股票账户"
    else:
        type_name = "模拟账户"
    display_name = f"{market_name}{type_name}"
    if int(metadata.get("same_type_count") or 0) > 1:
        display_name += f" · 尾号 {metadata['account_id_tail']}"
    return display_name


def render_experience_report(
    *,
    rows: list[Mapping[str, Any]],
    account_display_name: str,
    internal_account_label: str | None = None,
    owner_statuses: Mapping[str, str] | None = None,
) -> str:
    display = str(account_display_name or "").strip()
    fields = experience_fields(display)
    label = str(internal_account_label or "").strip()

    def clean(value: Any) -> str:
        value_text = str(value or "").replace("|", "\\|").replace("\n", " ").strip()
        if label:
            value_text = re.sub(
                rf"(?<![A-Za-z0-9_-]){re.escape(label)}(?![A-Za-z0-9_-])",
                "<account>",
                value_text,
                flags=re.IGNORECASE,
            )
        return value_text

    labels = {
        "opening": "Sell Put / Covered Call",
        "sp_lc": "Combo Yield SP+LC",
        "cc_lp": "Combo Yield CC+LP",
    }
    status_lines = [
        f"- 正式状态（{label_text}）：{clean((owner_statuses or {}).get(owner))}"
        for owner, label_text in labels.items()
        if str((owner_statuses or {}).get(owner) or "").strip()
    ]
    if not status_lines:
        status_lines = ["- 正式状态：本次没有可扫描的策略范围"]

    lines = [
        "# Options Monitor 体验扫描",
        "",
        EXPERIENCE_BANNER,
        "",
        f"- 账户：{display}",
        f"- 扫描模式：{fields['scan_mode']}",
        f"- 容量来源：{fields['capacity_source']}",
        "- 可执行：否",
        *status_lines,
        "",
        "| 标的 | 策略 | 候选数 | 结果摘要 |",
        "|---|---|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {symbol} | {strategy} | {count} | {note} |".format(
                symbol=clean(row.get("symbol")) or "-",
                strategy=clean(row.get("strategy")) or "-",
                count=clean(row.get("candidate_count")) or "0",
                note=clean(row.get("note")) or "-",
            )
        )
    if not rows:
        lines.append("| - | - | 0 | 本次没有可扫描的策略范围 |")
    return "\n".join(lines) + "\n"
