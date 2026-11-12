"""Conclusion-first weekly review of configured strike targets and frozen rejects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from domain.domain.symbol_identity import canonical_symbol
from src.application.agent_tool_contracts import AgentToolError
from src.application.reports.prices import (
    PriceSnapshot,
    resolve_price_snapshot,
)


from src.application.reports.strike_metrics import (
    DIRECTIONS, RULES, assess_boundary, collect_candidate_evidence,
    load_candidate_evidence, report_conclusion, summarize_rejections,
)


CONFIG_REVIEW_SCHEMA_VERSION = "config_review.v2"
_MARKET_FILES: tuple[tuple[str, str], ...] = (("HK", "user.hk.json"), ("US", "user.us.json"))


@dataclass(frozen=True)
class ConfigReviewResult:
    markdown: str
    payload: dict[str, Any]
    summary: dict[str, Any]
    evidence: dict[str, Any]


def build_config_review(
    *,
    configs_root: str | Path,
    runtime_root: str | Path,
    prices_file: str | Path | None = None,
    candidate_evidence_file: str | Path | None = None,
    now: datetime | None = None,
) -> ConfigReviewResult:
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = Path(configs_root).expanduser().resolve()
    markets, config_files, data_gaps, system_config = _load_market_configs(root)
    if not markets:
        raise AgentToolError(
            code="REPORT_CONFIG_MISSING",
            message="no market user config was found",
            details={"configs_root": str(root), "expected": [name for _, name in _MARKET_FILES]},
            hint="Pass --configs-root or create configs/user.hk.json and configs/user.us.json.",
        )
    snapshot = resolve_price_snapshot(
        runtime_root=runtime_root,
        prices_file=prices_file,
        markets=[item["market"] for item in markets],
    )
    market_payloads: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for market in markets:
        rows = [
            _symbol_payload(
                market=market["market"],
                symbol_cfg=symbol_cfg,
                config_path=market["path"],
                snapshot=snapshot,
                symbol_defaults=market.get("symbol_defaults") or {},
            )
            for symbol_cfg in market["symbols"]
        ]
        all_rows.extend(rows)
        market_payloads.append({"market": market["market"], "symbols": rows})
    evidence = (load_candidate_evidence(candidate_evidence_file, generated_at) if candidate_evidence_file
                else collect_candidate_evidence(runtime_root, generated_at))
    rejections = summarize_rejections(evidence)
    if rejections["coverage"] != "complete":
        data_gaps.append("指标A：方向化拒绝证据不足（旧快照或数据缺口），不能视为0")
    assessments = _assess_boundaries(all_rows, evidence=evidence, now=generated_at)
    conclusion = report_conclusion(assessments, data_gaps)
    summary = _summary(all_rows, snapshot=snapshot, data_gaps=data_gaps)
    summary.update(conclusion=conclusion["state"], strike_rejection_coverage=rejections["coverage"])
    payload = {
        "period": evidence["period"], "conclusion": conclusion, "rules": dict(RULES),
        "strike_rejections": rejections, "boundary_assessments": assessments,
        "schema_version": CONFIG_REVIEW_SCHEMA_VERSION,
        "generated_at_utc": _iso(generated_at),
        "configs_root": str(root),
        "config_files": config_files,
        "system_config": system_config,
        "price_source": snapshot.source,
        "price_observed_at_utc": snapshot.observed_at_utc,
        "data_gaps": data_gaps,
        "summary": summary,
        "markets": market_payloads,
    }
    markdown = _render_markdown(
        generated_at=generated_at,
        config_root=root,
        config_files=config_files,
        system_config=system_config,
        snapshot=snapshot,
        markets=market_payloads,
        summary=summary,
        data_gaps=data_gaps,
    )
    markdown = _render_review_sections(payload) + "\n" + markdown.partition("\n")[2].lstrip()
    return ConfigReviewResult(markdown=markdown, payload=payload, summary=summary, evidence=evidence)


def _load_market_configs(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    config_files: list[dict[str, Any]] = []
    data_gaps: list[str] = []
    system_path = root / "system.json"
    system_payload: dict[str, Any] | None = None
    system_config: dict[str, Any] = {"path": str(system_path), "exists": False}
    if system_path.is_file():
        try:
            loaded = json.loads(system_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AgentToolError(
                code="REPORT_CONFIG_INVALID",
                message=f"failed to read system config: {system_path}",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        if isinstance(loaded, dict):
            system_payload = loaded
            system_config = {"path": str(system_path), "exists": True}
        else:
            raise AgentToolError(code="REPORT_CONFIG_INVALID", message=f"system config must be a JSON object: {system_path}")
    if system_payload is None:
        data_gaps.append("system.json 不存在；未声明的 enabled/DTE 不会继承市场默认值")
    for market, filename in _MARKET_FILES:
        path = root / filename
        if not path.is_file():
            config_files.append({"market": market, "path": str(path), "exists": False, "symbol_count": 0})
            data_gaps.append(f"{market}：配置文件不存在（{filename}）")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AgentToolError(
                code="REPORT_CONFIG_INVALID",
                message=f"failed to read {market} user config: {path}",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        if not isinstance(payload, dict):
            raise AgentToolError(code="REPORT_CONFIG_INVALID", message=f"{market} user config must be a JSON object: {path}")
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            raise AgentToolError(code="REPORT_CONFIG_INVALID", message=f"{market} user config must contain a symbols array: {path}")
        seen: set[str] = set()
        for index, symbol_cfg in enumerate(symbols):
            if not isinstance(symbol_cfg, dict):
                raise AgentToolError(
                    code="REPORT_CONFIG_INVALID",
                    message=f"{market} symbols[{index}] must be an object",
                )
            raw_symbol = str(symbol_cfg.get("symbol") or "").strip()
            if not raw_symbol:
                raise AgentToolError(
                    code="REPORT_CONFIG_INVALID",
                    message=f"{market} symbols[{index}].symbol is required",
                )
            try:
                key = canonical_symbol(raw_symbol)
            except Exception as exc:
                raise AgentToolError(
                    code="REPORT_CONFIG_INVALID",
                    message=f"{market} symbols[{index}].symbol is invalid: {raw_symbol}",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                ) from exc
            if key in seen:
                raise AgentToolError(
                    code="REPORT_CONFIG_INVALID",
                    message=f"{market} contains duplicate symbol: {raw_symbol}",
                )
            seen.add(key)
        markets.append(
            {
                "market": market,
                "path": path,
                "symbols": symbols,
                "symbol_defaults": _market_symbol_defaults(system_payload, market),
            }
        )
        config_files.append(
            {"market": market, "path": str(path), "exists": True, "symbol_count": len(symbols)}
        )
    return markets, config_files, data_gaps, system_config


def _market_symbol_defaults(system_payload: dict[str, Any] | None, market: str) -> dict[str, Any]:
    if not isinstance(system_payload, dict):
        return {}
    markets = system_payload.get("markets")
    if not isinstance(markets, dict):
        return {}
    section = markets.get(market.lower())
    if not isinstance(section, dict):
        return {}
    defaults = section.get("symbol_defaults")
    return defaults if isinstance(defaults, dict) else {}


def _symbol_payload(
    *,
    market: str,
    symbol_cfg: dict[str, Any],
    config_path: Path,
    snapshot: PriceSnapshot,
    symbol_defaults: dict[str, Any],
) -> dict[str, Any]:
    raw_symbol = str(symbol_cfg.get("symbol") or "").strip()
    key = canonical_symbol(raw_symbol)
    errors: list[str] = []
    use = _use_list(symbol_cfg.get("use"), f"{config_path.name}:{raw_symbol}.use", errors)
    put_section = _section(symbol_cfg, "sell_put", f"{raw_symbol}.sell_put", errors)
    call_section = _section(symbol_cfg, "sell_call", f"{raw_symbol}.sell_call", errors)
    combo_section = _section(symbol_cfg, "combo_yield", f"{raw_symbol}.combo_yield", errors)
    combo_call = _section(combo_section or {}, "call", f"{raw_symbol}.combo_yield.call", errors) if combo_section else None

    put = _side_payload(
        put_section,
        f"{raw_symbol}.sell_put",
        errors,
        defaults=_side_defaults(symbol_defaults, "sell_put"),
    )
    call = _side_payload(
        call_section,
        f"{raw_symbol}.sell_call",
        errors,
        defaults=_side_defaults(symbol_defaults, "sell_call"),
    )
    combo = _combo_payload(
        combo_section,
        combo_call,
        f"{raw_symbol}.combo_yield",
        errors,
        defaults=symbol_defaults.get("combo_yield") if isinstance(symbol_defaults.get("combo_yield"), dict) else {},
    )
    if errors:
        raise AgentToolError(
            code="REPORT_CONFIG_INVALID",
            message=f"invalid boundary configuration for {raw_symbol}",
            details={"errors": errors, "config_path": str(config_path)},
        )

    observation = snapshot.get(raw_symbol)
    price = (
        {
            "value": observation.price,
            "source": observation.source,
            "observed_at_utc": observation.observed_at_utc,
            "source_path": observation.source_path,
        }
        if observation is not None
        else None
    )
    distances: list[dict[str, Any]] = []
    if price is not None:
        distances.extend(_distance_entries(put=put, call=call, combo=combo, price=float(price["value"])))
    return {
        "symbol": raw_symbol,
        "canonical_symbol": key,
        "market": market,
        "use": use,
        "price": price,
        "strategies": {
            "sell_put": put,
            "sell_call": call,
            "combo_yield": combo,
        },
        "distances": distances,
    }


def _side_payload(
    section: dict[str, Any] | None,
    path: str,
    errors: list[str],
    *,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    if section is None:
        return {
            "configured": False,
            "enabled": None,
            "enabled_source": "unset",
            "min_strike": None,
            "max_strike": None,
            "min_dte": None,
            "max_dte": None,
            "dte_source": "unset",
        }
    enabled, enabled_source = _resolve_field(section, defaults, "enabled", f"{path}.enabled", errors, kind="bool")
    min_strike, _ = _resolve_field(section, defaults, "min_strike", f"{path}.min_strike", errors, kind="number")
    max_strike, _ = _resolve_field(section, defaults, "max_strike", f"{path}.max_strike", errors, kind="number")
    min_dte, dte_source = _resolve_field(section, defaults, "min_dte", f"{path}.min_dte", errors, kind="int")
    max_dte, max_dte_source = _resolve_field(section, defaults, "max_dte", f"{path}.max_dte", errors, kind="int")
    if max_dte_source == "market_default":
        dte_source = "market_default"
    return {
        "configured": True,
        "enabled": enabled,
        "enabled_source": enabled_source,
        "min_strike": min_strike,
        "max_strike": max_strike,
        "min_dte": min_dte,
        "max_dte": max_dte,
        "dte_source": dte_source,
    }


def _combo_payload(
    section: dict[str, Any] | None,
    call_section: dict[str, Any] | None,
    path: str,
    errors: list[str],
    *,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    if section is None:
        return {
            "configured": False,
            "enabled": None,
            "enabled_source": "unset",
            "call_min_strike": None,
            "call_max_strike": None,
            "min_dte": None,
            "max_dte": None,
            "dte_source": "unset",
        }
    call_defaults = defaults.get("call") if isinstance(defaults.get("call"), dict) else {}
    enabled, enabled_source = _resolve_field(section, defaults, "enabled", f"{path}.enabled", errors, kind="bool")
    min_dte, dte_source = _resolve_field(section, defaults, "min_dte", f"{path}.min_dte", errors, kind="int")
    max_dte, max_dte_source = _resolve_field(section, defaults, "max_dte", f"{path}.max_dte", errors, kind="int")
    if max_dte_source == "market_default":
        dte_source = "market_default"
    call_min_strike, _ = _resolve_field(
        call_section or {}, call_defaults, "min_strike", f"{path}.call.min_strike", errors, kind="number"
    )
    call_max_strike, _ = _resolve_field(
        call_section or {}, call_defaults, "max_strike", f"{path}.call.max_strike", errors, kind="number"
    )
    return {
        "configured": True,
        "enabled": enabled,
        "enabled_source": enabled_source,
        "call_min_strike": call_min_strike,
        "call_max_strike": call_max_strike,
        "min_dte": min_dte,
        "max_dte": max_dte,
        "dte_source": dte_source,
    }


def _side_defaults(symbol_defaults: dict[str, Any], side: str) -> dict[str, Any]:
    value = symbol_defaults.get(side)
    return value if isinstance(value, dict) else {}


def _distance_entries(
    *,
    put: dict[str, Any],
    call: dict[str, Any],
    combo: dict[str, Any],
    price: float,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for side_name, side, boundary_key, label_prefix in (
        ("sell_put", put, "max_strike", "卖出看跌上限"),
        ("sell_put", put, "min_strike", "卖出看跌下限"),
        ("sell_call", call, "min_strike", "卖出看涨下限"),
        ("sell_call", call, "max_strike", "卖出看涨上限"),
    ):
        value = side.get(boundary_key)
        if value is None or value <= 0:
            continue
        binding = None
        if side_name == "sell_put" and boundary_key == "max_strike":
            binding = "config_bound" if price > value else "spot_bound"
        elif side_name == "sell_call" and boundary_key == "min_strike":
            binding = "config_bound" if price < value else "spot_bound"
        entries.append(_distance_entry(
            path=f"{side_name}.{boundary_key}",
            label=f"{label_prefix}",
            value=float(value),
            price=price,
            binding=binding,
        ))
    for boundary_key, label in (("call_min_strike", "Combo 看涨下限"), ("call_max_strike", "Combo 看涨上限")):
        value = combo.get(boundary_key)
        if value is None or value <= 0:
            continue
        entries.append(_distance_entry(
            path=f"combo_yield.call.{boundary_key.removeprefix('call_')}",
            label=label,
            value=float(value),
            price=price,
        ))
    return entries


def _distance_entry(*, path: str, label: str, value: float, price: float, binding: str | None = None) -> dict[str, Any]:
    signed_pct = (price - value) / value * 100.0
    relation = "above" if signed_pct > 0 else "below" if signed_pct < 0 else "equal"
    return {
        "path": path,
        "label": label,
        "value": value,
        "signed_pct": round(signed_pct, 2),
        "relation": relation,
        "binding": binding,
    }


def _assess_boundaries(rows: list[dict[str, Any]], *, evidence: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    assessments = []
    for row in rows:
        strategies = row["strategies"]
        for side in ("sell_put", "sell_call"):
            policy = strategies[side]
            for field in ("min_strike", "max_strike"):
                value = policy.get(field)
                if value is not None and value > 0:
                    assessments.append(assess_boundary(
                        market=row["market"], symbol=row["canonical_symbol"], path=f"{side}.{field}", value=float(value),
                        enabled=policy.get("enabled") is not False, price=row["price"], evidence=evidence, now=now,
                    ))
        combo = strategies["combo_yield"]
        for field in ("min_strike", "max_strike"):
            value = combo.get(f"call_{field}")
            if value is not None and value > 0:
                assessments.append(assess_boundary(
                    market=row["market"], symbol=row["canonical_symbol"], path=f"combo_yield.call.{field}", value=float(value),
                    enabled=combo.get("enabled") is True, price=row["price"], evidence=evidence, now=now,
                ))
    return assessments


def _render_review_sections(payload: dict[str, Any]) -> str:
    conclusion, metric = payload["conclusion"], payload["strike_rejections"]
    coverage_text = {"complete": "已留存快照完整可判读", "partial": "部分数据", "unavailable": "该期无数据"}
    lines = ["# 周度配置清单报告", "", f"**结论：{conclusion['state']}**", ""]
    lines.extend(f"- {reason}" for reason in conclusion["reasons"])
    lines.extend(["", f"统计周期：{payload['period']['label']}（Asia/Shanghai）。现价为本次生成时最近可用观察。", "",
                  "## 指标 A：strike 边界拒绝", "",
                  f"数据覆盖：**{coverage_text[metric['coverage']]}**；快照 {metric['snapshot_count']} 份，"
                  f"决策 {metric['decision_count']} 次，日期 {len(metric['dates'])} 个。",
                  f"已验证观测中的旧 hard_strike：{metric['legacy_hard_strike_count']} 次；未验证历史不计入，方向不可恢复，缺失不能当作0。",
                  f"快照读取/验证缺口：{len(metric['errors'])}；封存范围：{metric['sealed_at_min'] or '无'} ～ {metric['sealed_at_max'] or '无'}。", "",
                  "计数是账户扫描观测；同一合约会重复出现，不等于独立交易机会。完整性仅指已取得的留存快照。", "",
                  "| 市场 | 快照 | 方向拒绝观测 | 覆盖 |", "| --- | ---: | ---: | --- |"])
    for market in metric["markets"]:
        count = str(market["count"]) if market["count"] is not None else (f"至少 {market['observed_count']}（部分）" if market["observed_count"] else "该期无数据")
        lines.append(f"| {market['market']} | {market['snapshot_count']} | {count} | {coverage_text[market['coverage']]} |")
    if not metric["markets"]:
        lines.append("| — | 0 | 该期无数据 | 无可用快照 |")
    if metric["groups"]:
        lines.extend(["", "| 市场 | 标的 | 方向 | 已观测拒绝 | 不同合约 | 仅因 strike |",
                      "| --- | --- | --- | ---: | ---: | ---: |"])
        for group in metric["groups"]:
            lines.append(f"| {group['market']} | {group['symbol']} | {DIRECTIONS[group['reason']]} | {group['observed_count']} | {group['unique_contracts']} | {group['strike_only_count']} |")
    lines.extend(["", "## 指标 B：现价相对配置边界", "",
                  "距离 = (现价 − 边界) / 边界 × 100%；压力在 max 上界取距离、min 下界取负距离。价格时点为采集时点，不冒充成交时间。", "",
                  "| 市场/标的 | 配置边界 | 边界值 | 现价 | 距离 | 压力 | 同值边界 R/N | 结论与理由 |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"])
    for row in payload["boundary_assessments"]:
        price = row["price"]
        distance = f"{row['distance_pct']:+.2f}%" if row["distance_pct"] is not None else "无数据"
        pressure = f"{row['pressure_pct']:+.2f}%" if row["pressure_pct"] is not None else "无数据"
        lines.append(f"| {row['market']} {row['symbol']} | {row['path']} | {_fmt_number(row['value'])} | "
                     f"{_fmt_number(price['value']) if price else '无数据'} | {distance} | {pressure} | "
                     f"{row['R']}/{row['N']} | {row['conclusion']}：{row['reason']} |")
    lines.extend(["", "价格来源与采集时点："])
    seen = set()
    for row in payload["boundary_assessments"]:
        if (row["market"], row["symbol"]) in seen:
            continue
        seen.add((row["market"], row["symbol"]))
        price = row["price"] or {}
        lines.append(f"- {row['market']} {row['symbol']}：{price.get('source', '无数据')}；{price.get('observed_at_utc') or '时点未知'}")
    lines.extend(["", "判定门槛：至少30次可比决策、3个日期且证据完整、价格不超过96小时。建议人工复核须压力≥15%、"
                  "仅因strike拒绝≥20次且占比≥30%、至少5个合约和3个拒绝日期；无需调整须压力≤5%、拒绝率<10%；其余观察。",
                  "同值边界仅表示封存 policy/threshold 与当前配置相等，不证明配置是唯一原因，也不保证调整后候选通过。", "",
                  "## 配置清单与系统总览", ""])
    return "\n".join(lines)


def _summary(
    rows: list[dict[str, Any]],
    *,
    snapshot: PriceSnapshot,
    data_gaps: list[str],
) -> dict[str, Any]:
    market_counts: dict[str, int] = {}
    for row in rows:
        market_counts[row["market"]] = market_counts.get(row["market"], 0) + 1
    price_rows = [row for row in rows if row.get("price")]
    put_cap = [row for row in rows if (row["strategies"]["sell_put"].get("max_strike")) is not None]
    call_floor = [row for row in rows if (row["strategies"]["sell_call"].get("min_strike")) is not None]
    combo_enabled = [row for row in rows if row["strategies"]["combo_yield"].get("enabled") is True]
    put_config_bound = [
        row["symbol"]
        for row in price_rows
        for item in row["distances"]
        if item["path"] == "sell_put.max_strike" and item.get("binding") == "config_bound"
    ]
    put_spot_bound = [
        row["symbol"]
        for row in price_rows
        for item in row["distances"]
        if item["path"] == "sell_put.max_strike" and item.get("binding") == "spot_bound"
    ]
    call_config_bound = [
        row["symbol"]
        for row in price_rows
        for item in row["distances"]
        if item["path"] == "sell_call.min_strike" and item.get("binding") == "config_bound"
    ]
    call_spot_bound = [
        row["symbol"]
        for row in price_rows
        for item in row["distances"]
        if item["path"] == "sell_call.min_strike" and item.get("binding") == "spot_bound"
    ]
    return {
        "symbol_count": len(rows),
        "market_counts": dict(sorted(market_counts.items())),
        "price_available": len(price_rows),
        "price_missing": len(rows) - len(price_rows),
        "price_source": snapshot.source,
        "price_observed_at_utc": snapshot.observed_at_utc,
        "put_cap_configured": len(put_cap),
        "call_floor_configured": len(call_floor),
        "combo_enabled": len(combo_enabled),
        "put_config_bound": put_config_bound,
        "put_spot_bound": put_spot_bound,
        "call_config_bound": call_config_bound,
        "call_spot_bound": call_spot_bound,
        "data_gaps": list(data_gaps),
    }


def _render_markdown(
    *,
    generated_at: datetime,
    config_root: Path,
    config_files: list[dict[str, Any]],
    snapshot: PriceSnapshot,
    markets: list[dict[str, Any]],
    summary: dict[str, Any],
    data_gaps: list[str],
    system_config: dict[str, Any],
) -> str:
    lines: list[str] = [
        "# 周度配置清单报告",
        "",
        f"- 生成时间：{_iso(generated_at)}",
        f"- 配置目录：`{config_root}`",
        "- 清单范围：用户配置中的行权价边界与本次现价；上方指标A列出上一完整周的边界拒绝观测与数据覆盖。",
        "- 距离定义：(现价 − 边界) ÷ 边界 × 100%；正数表示现价高于边界，负数表示现价低于边界。",
        "",
        "## 数据来源",
        "",
    ]
    for item in config_files:
        status = "已读取" if item["exists"] else "不存在"
        lines.append(f"- {item['market']}：`{item['path']}`（{status}，{item['symbol_count']} 个标的）")
    if isinstance(system_config, dict):
        status = "已读取" if system_config.get("exists") else "不存在"
        lines.append(
            f"- system.json：`{system_config.get('path')}`（{status}；仅用于用户配置未声明字段的回退）"
        )
    price_source = _price_source_text(snapshot)
    lines.append(f"- 现价：{price_source}")
    if data_gaps:
        lines.append("- 数据缺口：")
        lines.extend(f"  - {gap}" for gap in data_gaps)
    lines.append("")

    lines.extend(
        [
            "## 摘要",
            "",
            f"- 标的：{summary['symbol_count']}（"
            + " / ".join(f"{market} {count}" for market, count in summary["market_counts"].items())
            + "）",
            f"- 现价覆盖：{summary['price_available']}/{summary['symbol_count']}；缺失 {summary['price_missing']}",
            f"- 卖出看跌上限已配置：{summary['put_cap_configured']} 个标的",
            f"- 卖出看涨下限已配置：{summary['call_floor_configured']} 个标的",
            f"- Combo Yield 已启用：{summary['combo_enabled']} 个标的",
        ]
    )
    if summary["put_config_bound"]:
        lines.append(f"- 现价高于卖出看跌上限（配置上限生效）：{'、'.join(summary['put_config_bound'])}")
    if summary["put_spot_bound"]:
        lines.append(f"- 现价低于卖出看跌上限（现价成为实际上限）：{'、'.join(summary['put_spot_bound'])}")
    if summary["call_config_bound"]:
        lines.append(f"- 现价低于卖出看涨下限（配置下限生效）：{'、'.join(summary['call_config_bound'])}")
    if summary["call_spot_bound"]:
        lines.append(f"- 现价高于卖出看涨下限（现价成为实际下限）：{'、'.join(summary['call_spot_bound'])}")
    lines.append("")

    for market in markets:
        lines.extend([f"## {'港股' if market['market'] == 'HK' else '美股'}（{market['market']}）", ""])
        lines.append("| 标的 | 现价 | 卖出看跌上限 | 现价相对上限 | 卖出看涨下限 | 现价相对下限 | Combo Yield |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in market["symbols"]:
            lines.append(_table_row(row))
        lines.append("")
        lines.append("### 明细")
        lines.append("")
        for row in market["symbols"]:
            lines.extend(_detail_lines(row))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _table_row(row: dict[str, Any]) -> str:
    put = row["strategies"]["sell_put"]
    call = row["strategies"]["sell_call"]
    combo = row["strategies"]["combo_yield"]
    price = row.get("price")
    put_distance = _distance_for(row, "sell_put.max_strike")
    call_distance = _distance_for(row, "sell_call.min_strike")
    return "| " + " | ".join(
        [
            row["symbol"],
            f"{price['value']:.2f}" if price else "—",
            _boundary_cell(put, "max_strike"),
            _distance_cell(put_distance) if price else "—",
            _boundary_cell(call, "min_strike"),
            _distance_cell(call_distance) if price else "—",
            _enabled_cell(combo),
        ]
    ) + " |"


def _boundary_cell(side: dict[str, Any], key: str) -> str:
    if not side.get("configured"):
        return "未配置"
    if side.get("enabled") is False:
        return "停用"
    value = side.get(key)
    return _fmt_number(value) if value is not None else "未设置"


def _enabled_cell(section: dict[str, Any]) -> str:
    if not section.get("configured"):
        return "未配置"
    enabled = section.get("enabled")
    if enabled is True:
        return "启用"
    if enabled is False:
        return "停用"
    return "未声明"


def _distance_for(row: dict[str, Any], path: str) -> dict[str, Any] | None:
    return next((item for item in row.get("distances") or [] if item.get("path") == path), None)


def _distance_cell(item: dict[str, Any] | None) -> str:
    if item is None:
        return "—"
    relation = item.get("relation")
    pct = abs(float(item.get("signed_pct") or 0.0))
    if relation == "above":
        return f"高于 {pct:.1f}%"
    if relation == "below":
        return f"低于 {pct:.1f}%"
    return "持平"


def _detail_lines(row: dict[str, Any]) -> list[str]:
    lines = [f"#### {row['symbol']}"]
    use = "、".join(row.get("use") or []) or "未声明"
    lines.append(f"- 策略模板：{use}")
    price = row.get("price")
    if price:
        observed = price.get("observed_at_utc") or "时间未知"
        lines.append(f"- 现价：{price['value']:.2f}（{price.get('source')}，观察于 {observed}）")
    else:
        lines.append("- 现价：不可用（没有最新 brief 观察或未提供价格文件）")
    for label, side_name, side in (
        ("卖出看跌", "sell_put", row["strategies"]["sell_put"]),
        ("卖出看涨", "sell_call", row["strategies"]["sell_call"]),
    ):
        if not side.get("configured"):
            lines.append(f"- {label}：未配置")
            continue
        parts = [
            _boundary_text(row, f"{side_name}.min_strike", "下限", side.get("min_strike")),
            _boundary_text(row, f"{side_name}.max_strike", "上限", side.get("max_strike")),
            f"DTE {_dte_label(side)}",
        ]
        lines.append(f"- {label}（{_state_text(side)}）：" + "；".join(parts))
    combo = row["strategies"]["combo_yield"]
    if not combo.get("configured"):
        lines.append("- Combo Yield：未配置")
    else:
        parts = [
            _boundary_text(row, "combo_yield.call.min_strike", "看涨下限", combo.get("call_min_strike")),
            _boundary_text(row, "combo_yield.call.max_strike", "看涨上限", combo.get("call_max_strike")),
            f"DTE {_dte_label(combo)}",
        ]
        lines.append(f"- Combo Yield（{_state_text(combo)}）：" + "；".join(parts))
    return lines


def _state_text(section: dict[str, Any]) -> str:
    enabled = section.get("enabled")
    if enabled is True:
        text = "启用"
    elif enabled is False:
        text = "停用"
    else:
        return "未声明"
    if section.get("enabled_source") == "market_default":
        text += "，继承市场默认"
    return text


def _dte_label(section: dict[str, Any]) -> str:
    text = _dte_text(section.get("min_dte"), section.get("max_dte"))
    if section.get("dte_source") == "market_default":
        text += "（继承）"
    return text


def _boundary_text(row: dict[str, Any], path: str, label: str, value: float | None) -> str:
    if value is None:
        return f"{label}未设置"
    text = f"{label} {_fmt_number(value)}"
    item = _distance_for(row, path)
    if item is not None:
        text += f"（{_distance_cell(item)}"
        binding = item.get("binding")
        if binding == "config_bound":
            text += "；配置边界生效"
        elif binding == "spot_bound":
            text += "；现价成为实际边界"
        text += "）"
    return text


def _dte_text(min_dte: int | None, max_dte: int | None) -> str:
    if min_dte is None and max_dte is None:
        return "未设置"
    if min_dte is not None and max_dte is not None:
        return f"{min_dte}–{max_dte}"
    if min_dte is not None:
        return f"≥{min_dte}"
    return f"≤{max_dte}"


def _price_source_text(snapshot: PriceSnapshot) -> str:
    if not snapshot.observations:
        if snapshot.source.startswith("prices_file:"):
            return f"`{snapshot.source.removeprefix('prices_file:')}`（无可用价格）"
        return "runtime daily_decision_brief（无可用观察）"
    observed = snapshot.observed_at_utc or "时间未知"
    if snapshot.source.startswith("prices_file:"):
        return f"`{snapshot.source.removeprefix('prices_file:')}`（生成于 {observed}）"
    return f"runtime daily_decision_brief 最新观察（{observed}）"


def _section(symbol_cfg: dict[str, Any], key: str, path: str, errors: list[str]) -> dict[str, Any] | None:
    value = symbol_cfg.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return None
    return value


def _resolve_field(
    section: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
    path: str,
    errors: list[str],
    *,
    kind: str,
) -> tuple[Any, str]:
    if key in section and section.get(key) is not None:
        value: Any = section.get(key)
        source = "explicit"
    elif isinstance(defaults, dict) and defaults.get(key) is not None:
        value = defaults.get(key)
        source = "market_default"
    else:
        return None, "unset"
    if kind == "bool":
        if not isinstance(value, bool):
            errors.append(f"{path} must be a boolean")
            return None, "invalid"
        return value, source
    parsed = _coerce_number(value, path, errors)
    if parsed is None:
        return None, "invalid"
    if kind == "int":
        if not parsed.is_integer() or parsed < 0:
            errors.append(f"{path} must be a non-negative integer")
            return None, "invalid"
        return int(parsed), source
    return parsed, source


def _coerce_number(value: Any, path: str, errors: list[str]) -> float | None:
    if isinstance(value, bool):
        errors.append(f"{path} must be a number")
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        errors.append(f"{path} must be a number")
        return None
    if not math.isfinite(parsed):
        errors.append(f"{path} must be finite")
        return None
    return parsed


def _use_list(value: Any, path: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    errors.append(f"{path} must be a string or an array")
    return []


def _fmt_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["CONFIG_REVIEW_SCHEMA_VERSION", "ConfigReviewResult", "build_config_review"]
