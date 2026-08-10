from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from domain.domain.daily_decision_brief import effective_daily_brief_actionability
from src.application.ai_decision_advice.config import ADVICE_RECORDS_FILE
from src.application.ai_decision_advice.validation import (
    ACTIONS,
    INPUT_BINDING_KEYS,
    SCHEMA_NAME,
)
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.daily_decision_brief_renderer import render_query_brief
from src.application.daily_decision_brief_repository import (
    read_daily_decision_brief,
    read_latest_daily_decision_brief,
)
from src.application.runtime_paths import resolve_runtime_root
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
)


_MARKETS = ("US", "HK")
_MARKET_LABELS = {"US": "美股", "HK": "港股"}
_ADVICE_STATUSES = frozenset({"completed", "unavailable", "not_applicable"})
_FORMAL_DECISION_KEYS = frozenset(
    {
        "scope",
        "strategy_family",
        "symbol",
        "action",
        "baseline_candidate_id",
        "selected_candidate_id",
        "rationale",
        "source_refs",
    }
)
_RATIONALE_KEYS = frozenset(
    {"risk_mechanism", "candidate_effect", "decision_reason"}
)
_SOURCE_REF_KEYS = frozenset(
    {"internal_fact_refs", "external_evidence_refs"}
)
_DEMOTION_KEYS = frozenset({"scope", "from_action", "to_action", "reason"})
_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "daily_decision_brief_read.output.v1",
    "source_label": "OM local daily_decision_brief.v1 successful current state",
    "result_shape": "single_brief_or_aggregate_sections",
    "fact_fields": [
        "available",
        "reason",
        "query",
        "sections[]",
        "brief.schema_version",
        "brief.brief_id",
        "brief.market",
        "brief.market_trading_date",
        "brief.account",
        "brief.revision",
        "brief.actionability",
        "effective_actionability",
        "coverage",
        "source",
        "freshness",
        "brief.actions[]",
        "brief.positions[]",
        "brief.funds",
        "brief.capacity",
        "brief.candidates",
        "brief.rejections",
        "brief.events[]",
        "brief.data_gaps[]",
        "brief.ai_decision_advice.status",
        "brief.ai_decision_advice.formal_record",
        "brief.ai_decision_advice.input_bindings",
        "brief.ai_decision_advice.actions[]",
        "brief.ai_decision_advice.validation",
        "brief.ai_decision_advice.versions",
        "brief.ai_decision_advice_evidence_index",
    ],
    "freshness_fields": [
        "freshness.data_as_of_utc",
        "freshness.valid_until_utc",
        "freshness.effective_actionability",
    ],
    "missing_data_fields": ["brief.data_gaps[]", "reason", "sections[].reason"],
    "model_preview_fields": ["query", "available", "effective_actionability", "sections", "rendered_markdown"],
}


def read_daily_brief_view(
    *,
    base: Path,
    account: str | None = None,
    market: str | None = None,
    market_trading_date: str | None = None,
    revision: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    account_norm = str(account or "").strip().lower() or None
    market_norm = str(market or "").strip().upper() or None
    date_norm = str(market_trading_date or "").strip() or None
    now = _utc_datetime(now_utc)
    if market_norm is not None and market_norm not in _MARKETS:
        raise ValueError("market must be US or HK")
    if revision is not None and revision < 0:
        raise ValueError("revision must be non-negative")
    if revision is not None and date_norm is None:
        raise ValueError("market_trading_date is required when revision is provided")
    if date_norm is not None and (account_norm is None or market_norm is None):
        raise ValueError("account and market are required for day or revision queries")

    if date_norm is not None or (account_norm is not None and market_norm is not None):
        assert account_norm is not None and market_norm is not None
        return _read_single_daily_brief_view(
            base=base,
            account=account_norm,
            market=market_norm,
            market_trading_date=date_norm,
            revision=revision,
            now_utc=now,
        )

    scopes = _enabled_daily_brief_scopes(account=account_norm, market=market_norm)
    sections = [
        _read_single_daily_brief_view(
            base=base,
            account=scope_account,
            market=scope_market,
            market_trading_date=None,
            revision=None,
            now_utc=now,
            heading_level=2,
        )
        for scope_account, scope_market in scopes
    ]
    return _aggregate_daily_brief_view(
        sections=sections,
        account=account_norm,
        market=market_norm,
        now_utc=now,
    )


def _read_single_daily_brief_view(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str | None,
    revision: int | None,
    now_utc: datetime,
    heading_level: int = 1,
) -> dict[str, Any]:
    if market_trading_date is None:
        result = read_latest_daily_decision_brief(base=base, account=account, market=market)
        mode = "latest"
    else:
        result = read_daily_decision_brief(
            base=base,
            account=account,
            market=market,
            market_trading_date=market_trading_date,
            revision=revision,
        )
        mode = "revision" if revision is not None else "day_latest"

    query = {
        "mode": mode,
        "account": account,
        "market": market,
        "market_trading_date": market_trading_date,
        "revision": revision,
    }
    if not bool(result.get("available")):
        reason = str(result.get("reason") or "unavailable")
        effective = "unavailable"
        return {
            "schema_version": "daily_decision_brief_read.output.v1",
            "available": False,
            "reason": reason,
            "query": query,
            "brief": None,
            "effective_actionability": effective,
            "coverage": _coverage(None, reason=reason),
            "source": _source(result),
            "freshness": _freshness(None, effective_actionability=effective),
            "rendered_markdown": _render_unavailable(query=query, reason=reason, heading_level=heading_level),
        }

    brief = _with_formal_ai_decision_advice(
        base=base,
        brief=dict(result["brief"]),
    )
    effective = effective_daily_brief_actionability(brief, now_utc=now_utc)
    rendered_brief = dict(brief)
    rendered_brief["actionability"] = effective
    return {
        "schema_version": "daily_decision_brief_read.output.v1",
        "available": True,
        "reason": "ok",
        "query": query,
        "brief": brief,
        "effective_actionability": effective,
        "coverage": _coverage(brief, reason="ok"),
        "source": _source(result),
        "freshness": _freshness(brief, effective_actionability=effective),
        "rendered_markdown": render_query_brief(
            rendered_brief,
            context={"query_time_utc": now_utc.isoformat()},
            heading_level=heading_level,
        ),
    }


def _with_formal_ai_decision_advice(
    *,
    base: Path,
    brief: dict[str, Any],
) -> dict[str, Any]:
    section = brief.get("ai_decision_advice")
    if not isinstance(section, Mapping):
        return brief

    enriched = dict(section)
    enriched.update(
        {
            "formal_record": {
                "available": False,
                "reason": "advice_record_not_referenced",
            },
            "input_bindings": {},
            "actions": [],
            "validation": {
                "demotions": [],
                "repair_attempted": False,
            },
            "versions": {},
            "reuse_of_advice_id": None,
        }
    )
    advice_id = str(section.get("advice_record_id") or "").strip()
    run_id = str(brief.get("run_id") or "").strip()
    account = str(brief.get("account") or "").strip().lower()
    market = str(brief.get("market") or "").strip().upper()
    if not advice_id or not run_id or not account or not market:
        result = dict(brief)
        result["ai_decision_advice"] = enriched
        return result

    try:
        payload = read_account_run_state_bytes_safely(
            base=base,
            run_id=run_id,
            account=account,
            name=ADVICE_RECORDS_FILE,
        ).decode("utf-8")
        records = _parse_formal_advice_records(payload)
    except (AccountRunConfigError, OSError, UnicodeDecodeError, ValueError):
        enriched["formal_record"] = {
            "available": False,
            "reason": "formal_record_unavailable",
        }
    else:
        same_id = [row for row in records if row.get("advice_id") == advice_id]
        if len(same_id) != 1:
            enriched["formal_record"] = {
                "available": False,
                "reason": (
                    "formal_record_ambiguous"
                    if same_id
                    else "formal_record_not_found"
                ),
            }
        else:
            record = same_id[0]
            identity_matches = (
                record.get("run_id") == run_id
                and str(record.get("market") or "").strip().upper() == market
                and str(record.get("status") or "").strip().lower()
                == str(section.get("status") or "").strip().lower()
            )
            formal_view = _formal_advice_view(record) if identity_matches else None
            if not identity_matches:
                enriched["formal_record"] = {
                    "available": False,
                    "reason": "formal_record_identity_mismatch",
                }
            elif formal_view is None:
                enriched["formal_record"] = {
                    "available": False,
                    "reason": "formal_record_invalid",
                }
            elif not _formal_record_matches_brief(record, section):
                enriched["formal_record"] = {
                    "available": False,
                    "reason": "formal_record_identity_mismatch",
                }
            else:
                enriched.update(formal_view)

    result = dict(brief)
    result["ai_decision_advice"] = enriched
    return result


def _parse_formal_advice_records(payload: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("formal advice row must be an object")
        if row.get("kind") == "advice_record":
            records.append(row)
    return records


def _formal_record_matches_brief(
    record: Mapping[str, Any],
    section: Mapping[str, Any],
) -> bool:
    record_zero = record.get("zero_candidate")
    section_zero = section.get("zero_candidate")
    formal_decisions = _validated_formal_decisions(record)
    brief_decisions = _brief_decision_projection(section)
    return (
        (str(record.get("unavailable_reason") or "") or None)
        == (str(section.get("unavailable_reason") or "") or None)
        and (str(record.get("evidence_as_of") or "") or None)
        == (str(section.get("evidence_as_of") or "") or None)
        and bool(record.get("reused")) == bool(section.get("reused"))
        and isinstance(record_zero, Mapping)
        and isinstance(section_zero, Mapping)
        and {
            "sell_put": bool(record_zero.get("sell_put")),
            "covered_call": bool(record_zero.get("covered_call")),
        }
        == {
            "sell_put": bool(section_zero.get("sell_put")),
            "covered_call": bool(section_zero.get("covered_call")),
        }
        and formal_decisions is not None
        and brief_decisions is not None
        and formal_decisions == brief_decisions
    )


def _formal_advice_view(
    record: Mapping[str, Any],
) -> dict[str, Any] | None:
    status = str(record.get("status") or "").strip().lower()
    if not _formal_record_envelope_is_valid(record):
        return None
    raw_bindings = record.get("input_bindings")
    if (
        not isinstance(raw_bindings, Mapping)
        or set(raw_bindings) != set(INPUT_BINDING_KEYS)
        or any(
            not isinstance(raw_bindings.get(key), str)
            or not str(raw_bindings.get(key)).strip()
            for key in INPUT_BINDING_KEYS
        )
    ):
        return None
    bindings = {key: raw_bindings[key] for key in INPUT_BINDING_KEYS}
    raw_versions = record.get("versions")
    version_keys = ("provider", "model", "schema_name", "prompt_fingerprint")
    if not isinstance(raw_versions, Mapping) or any(
        not isinstance(raw_versions.get(key), str)
        or not str(raw_versions.get(key)).strip()
        for key in version_keys
    ) or raw_versions.get("schema_name") != SCHEMA_NAME:
        return None
    versions = {key: raw_versions[key] for key in version_keys}
    decisions = _validated_formal_decisions(record)
    if decisions is None:
        return None
    raw_demotions = record.get("demotions")
    if not isinstance(raw_demotions, list):
        return None
    demotions: list[dict[str, str]] = []
    for item in raw_demotions:
        if (
            not isinstance(item, Mapping)
            or set(item) != _DEMOTION_KEYS
            or any(
                not isinstance(item.get(key), str)
                or not str(item.get(key)).strip()
                for key in _DEMOTION_KEYS
            )
            or item.get("to_action") != "needs_review"
            or item.get("scope") not in decisions
        ):
            return None
        demotions.append({key: str(item[key]) for key in sorted(_DEMOTION_KEYS)})
    reuse_of_advice_id = record.get("reuse_of_advice_id")
    if reuse_of_advice_id is not None and (
        not isinstance(reuse_of_advice_id, str) or not reuse_of_advice_id.strip()
    ):
        return None
    if bool(record.get("reused")) != bool(reuse_of_advice_id):
        return None
    actions: list[dict[str, Any]] = []
    for _scope, raw in sorted(decisions.items()):
        refs = raw["source_refs"]
        actions.append(
            {
                "scope": raw["scope"],
                "strategy_family": raw["strategy_family"],
                "symbol": raw["symbol"],
                "action": raw["action"],
                "baseline_candidate_id": raw["baseline_candidate_id"],
                "selected_candidate_id": raw["selected_candidate_id"],
                "rationale": dict(raw["rationale"]),
                "internal_fact_refs": list(refs["internal_fact_refs"]),
                "external_evidence_refs": list(
                    refs["external_evidence_refs"]
                ),
            }
        )
    return {
        "formal_record": {
            "available": True,
            "reason": "ok",
            "advice_id": record.get("advice_id"),
            "recorded_at": record.get("recorded_at"),
            "evidence_as_of": record.get("evidence_as_of"),
        },
        "input_bindings": bindings,
        "actions": actions,
        "validation": {
            "demotions": demotions,
            "repair_attempted": bool(record.get("repair_attempted")),
        },
        "versions": versions,
        "reuse_of_advice_id": reuse_of_advice_id,
    }


def _formal_record_envelope_is_valid(record: Mapping[str, Any]) -> bool:
    status = str(record.get("status") or "").strip().lower()
    unavailable_reason = record.get("unavailable_reason")
    evidence_as_of = record.get("evidence_as_of")
    zero = record.get("zero_candidate")
    if (
        record.get("kind") != "advice_record"
        or record.get("schema") != SCHEMA_NAME
        or status not in _ADVICE_STATUSES
        or any(
            not isinstance(record.get(key), str)
            or not str(record.get(key)).strip()
            for key in (
                "advice_id",
                "run_id",
                "account_ref",
                "market",
                "recorded_at",
            )
        )
        or (evidence_as_of is not None and not isinstance(evidence_as_of, str))
        or (
            unavailable_reason is not None
            and (
                not isinstance(unavailable_reason, str)
                or not unavailable_reason.strip()
            )
        )
        or not isinstance(record.get("reused"), bool)
        or (
            "repair_attempted" in record
            and not isinstance(record.get("repair_attempted"), bool)
        )
        or not isinstance(zero, Mapping)
        or set(zero) != {"sell_put", "covered_call"}
        or any(not isinstance(zero.get(key), bool) for key in zero)
    ):
        return False
    if status == "completed":
        return unavailable_reason is None and not all(zero.values())
    if status == "not_applicable":
        return unavailable_reason == "zero_candidate" and all(zero.values())
    return isinstance(unavailable_reason, str) and not all(zero.values())


def _validated_formal_decisions(
    record: Mapping[str, Any],
) -> dict[str, dict[str, Any]] | None:
    status = str(record.get("status") or "").strip().lower()
    decisions = record.get("decisions")
    if status not in _ADVICE_STATUSES or not isinstance(decisions, Mapping):
        return None
    if status != "completed":
        return {} if not decisions else None

    projected: dict[str, dict[str, Any]] = {}
    for scope_key, raw in decisions.items():
        if (
            not isinstance(scope_key, str)
            or not scope_key
            or not isinstance(raw, Mapping)
            or set(raw) != _FORMAL_DECISION_KEYS
            or raw.get("scope") != scope_key
        ):
            return None
        family = raw.get("strategy_family")
        symbol = raw.get("symbol")
        if family == "sell_put":
            if scope_key != "sell_put" or symbol is not None:
                return None
        elif family == "covered_call":
            if (
                not isinstance(symbol, str)
                or not symbol.strip()
                or scope_key != f"covered_call:{symbol}"
            ):
                return None
        else:
            return None
        common = _validated_decision_common(raw)
        if common is None:
            return None
        projected[scope_key] = {
            "scope": scope_key,
            "strategy_family": family,
            "symbol": symbol,
            **common,
        }
    zero = record.get("zero_candidate")
    if not isinstance(zero, Mapping):
        return None
    has_sell_put = "sell_put" in projected
    has_covered_call = any(
        scope.startswith("covered_call:") for scope in projected
    )
    if has_sell_put == bool(zero.get("sell_put")):
        return None
    if has_covered_call == bool(zero.get("covered_call")):
        return None
    return projected


def _brief_decision_projection(
    section: Mapping[str, Any],
) -> dict[str, dict[str, Any]] | None:
    status = str(section.get("status") or "").strip().lower()
    zero = section.get("zero_candidate")
    if (
        status not in _ADVICE_STATUSES
        or not isinstance(zero, Mapping)
        or set(zero) != {"sell_put", "covered_call"}
        or any(not isinstance(zero.get(key), bool) for key in zero)
        or (status == "not_applicable" and not all(zero.values()))
        or (status != "not_applicable" and all(zero.values()))
    ):
        return None
    sell_put = section.get("sell_put")
    covered_call = section.get("covered_call")
    if status != "completed":
        if sell_put is not None or covered_call not in (None, []):
            return None
        return {}

    projected: dict[str, dict[str, Any]] = {}
    if bool(zero.get("sell_put")):
        if sell_put is not None:
            return None
    else:
        if not isinstance(sell_put, Mapping):
            return None
        common = _validated_decision_common(sell_put)
        if common is None:
            return None
        projected["sell_put"] = {
            "scope": "sell_put",
            "strategy_family": "sell_put",
            "symbol": None,
            **common,
        }

    if bool(zero.get("covered_call")):
        if covered_call not in (None, []):
            return None
    else:
        if not isinstance(covered_call, list) or not covered_call:
            return None
        for raw in covered_call:
            if not isinstance(raw, Mapping):
                return None
            symbol = raw.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                return None
            scope_key = f"covered_call:{symbol}"
            if scope_key in projected:
                return None
            common = _validated_decision_common(raw)
            if common is None:
                return None
            projected[scope_key] = {
                "scope": scope_key,
                "strategy_family": "covered_call",
                "symbol": symbol,
                **common,
            }
    return projected


def _validated_decision_common(
    raw: Mapping[str, Any],
) -> dict[str, Any] | None:
    action = raw.get("action")
    baseline = raw.get("baseline_candidate_id")
    selected = raw.get("selected_candidate_id")
    rationale = raw.get("rationale")
    refs = raw.get("source_refs")
    if (
        action not in ACTIONS
        or not isinstance(baseline, str)
        or not baseline.strip()
        or (
            selected is not None
            and (not isinstance(selected, str) or not selected.strip())
        )
        or not isinstance(rationale, Mapping)
        or set(rationale) != _RATIONALE_KEYS
        or any(not isinstance(rationale.get(key), str) for key in _RATIONALE_KEYS)
        or not isinstance(refs, Mapping)
        or set(refs) != _SOURCE_REF_KEYS
    ):
        return None
    internal_refs = refs.get("internal_fact_refs")
    external_refs = refs.get("external_evidence_refs")
    if (
        not isinstance(internal_refs, list)
        or not isinstance(external_refs, list)
        or any(not isinstance(ref, str) or not ref for ref in internal_refs)
        or any(not isinstance(ref, str) or not ref for ref in external_refs)
        or len(internal_refs) != len(set(internal_refs))
        or len(external_refs) != len(set(external_refs))
    ):
        return None
    if action == "keep" and selected != baseline:
        return None
    if action == "switch" and (selected is None or selected == baseline):
        return None
    if action in {"defer", "needs_review"} and selected is not None:
        return None
    return {
        "action": action,
        "baseline_candidate_id": baseline,
        "selected_candidate_id": selected,
        "rationale": {key: rationale[key] for key in sorted(_RATIONALE_KEYS)},
        "source_refs": {
            "internal_fact_refs": list(internal_refs),
            "external_evidence_refs": list(external_refs),
        },
    }


def _enabled_daily_brief_scopes(*, account: str | None, market: str | None) -> list[tuple[str, str]]:
    markets = (market,) if market is not None else _MARKETS
    scopes: set[tuple[str, str]] = set()
    for market_name in markets:
        _path, cfg = load_runtime_config(config_key=market_name.lower(), expected_market=market_name.lower())
        accounts = [str(item or "").strip().lower() for item in cfg.get("accounts") or []]
        for account_name in accounts:
            if account_name and (account is None or account_name == account):
                scopes.add((account_name, market_name))
    return sorted(scopes)


def _aggregate_daily_brief_view(
    *,
    sections: list[dict[str, Any]],
    account: str | None,
    market: str | None,
    now_utc: datetime,
) -> dict[str, Any]:
    available_count = sum(bool(item.get("available")) for item in sections)
    if not sections:
        reason = "scope_not_enabled"
    elif available_count == len(sections):
        reason = "ok"
    elif available_count:
        reason = "partial"
    else:
        reason = "not_found"
    query = {
        "mode": "latest",
        "account": account,
        "market": market,
        "market_trading_date": None,
        "revision": None,
        "scope": "enabled_runtime_config",
    }
    warning = ""
    if reason == "partial":
        warning = "提醒｜部分账户或市场的成功扫描快照暂不可用。"
    elif reason == "not_found":
        warning = "提醒｜当前启用范围还没有可用的成功扫描快照。"
    elif reason == "scope_not_enabled":
        warning = "提醒｜没有匹配的启用账户或市场。"
    rendered_parts = ["# 期权监控"]
    if warning:
        rendered_parts.extend(["", warning])
    for section in sections:
        rendered_parts.extend(["", "---", "", str(section["rendered_markdown"]).strip()])
    return {
        "schema_version": "daily_decision_brief_read.output.v1",
        "available": bool(available_count),
        "reason": reason,
        "query": query,
        "brief": None,
        "sections": sections,
        "effective_actionability": "mixed" if len(sections) > 1 else (
            str(sections[0].get("effective_actionability") or "unavailable") if sections else "unavailable"
        ),
        "coverage": {
            "status": "ready" if reason == "ok" else ("partial" if reason == "partial" else "unavailable"),
            "reason": reason,
            "section_count": len(sections),
            "available_section_count": available_count,
            "unavailable_section_count": len(sections) - available_count,
        },
        "source": {
            "label": "OM local daily_decision_brief.v1 successful current state",
            "state_paths": [item["source"]["state_path"] for item in sections],
        },
        "freshness": {
            "query_time_utc": now_utc.isoformat(),
            "effective_actionability": "mixed" if len(sections) > 1 else (
                str(sections[0].get("effective_actionability") or "unavailable") if sections else "unavailable"
            ),
        },
        "rendered_markdown": "\n".join(rendered_parts).strip(),
    }


def _coverage(brief: dict[str, Any] | None, *, reason: str) -> dict[str, Any]:
    payload = brief or {}
    return {
        "status": str(payload.get("status") or ("unavailable" if brief is None else "unknown")),
        "reason": reason,
        "action_count": _list_count(payload.get("actions")),
        "position_count": _list_count(payload.get("positions")),
        "data_gap_count": _list_count(payload.get("data_gaps")),
        "source_artifact_count": _list_count(payload.get("source_artifacts")),
    }


def _source(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "OM local daily_decision_brief.v1 successful current state",
        "state_path": mask_path(result.get("path")),
    }


def _freshness(brief: dict[str, Any] | None, *, effective_actionability: str) -> dict[str, Any]:
    payload = brief or {}
    return {
        "data_as_of_utc": payload.get("data_as_of_utc"),
        "valid_until_utc": payload.get("valid_until_utc"),
        "effective_actionability": effective_actionability,
    }


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _render_unavailable(*, query: dict[str, Any], reason: str, heading_level: int) -> str:
    title_mark = "#" * max(1, min(int(heading_level), 5))
    date_text = query.get("market_trading_date") or "最近成功扫描"
    return "\n".join(
        [
            f"{title_mark} OM · 决策简报 · {query['account']}",
            "",
            "状态｜当前查询",
            f"市场｜{_MARKET_LABELS.get(str(query['market']), '市场')}",
            f"范围｜{date_text}",
            "",
            f"结论｜当前查询不可用：暂时没有成功扫描快照（{reason}）。",
        ]
    )


def _utc_datetime(value: datetime | None) -> datetime:
    now = value or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _validate_daily_brief_input(payload: dict[str, Any]) -> None:
    has_date = bool(str(payload.get("date") or "").strip())
    has_revision = payload.get("revision") is not None
    if has_revision and not has_date:
        raise AgentToolError(code="INPUT_ERROR", message="date is required when revision is provided")
    if (has_date or has_revision) and not str(payload.get("account") or "").strip():
        raise AgentToolError(code="INPUT_ERROR", message="account is required for day or revision queries")


def _daily_brief_read_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    revision_value = payload.get("revision")
    revision = None if revision_value is None else int(revision_value)
    date = str(payload.get("date") or "").strip() or None
    market = str(payload.get("market") or "").strip() or ("US" if date is not None else None)
    repo_root = repo_base()
    runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
    try:
        data = read_daily_brief_view(
            base=runtime_root,
            account=(str(payload.get("account") or "").strip() or None),
            market=market,
            market_trading_date=date,
            revision=revision,
        )
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    warnings = [] if data["reason"] == "ok" else [f"daily brief query status: {data['reason']}"]
    state_paths = data["source"].get("state_paths")
    meta = {"read_only": True}
    if isinstance(state_paths, list):
        meta["state_paths"] = state_paths
    else:
        meta["state_path"] = data["source"]["state_path"]
    return data, warnings, meta


DAILY_DECISION_BRIEF_READ_TOOL = build_agent_tool(
    name="daily_decision_brief_read",
    description=(
        "Read the latest successful option-monitor snapshot, a trading day, or an exact revision. "
        "Use for queries such as 期权监控, 最新期权报告, 港股期权, 美股期权, or lx/sy 期权. "
        "Omitting account and market returns all enabled scopes. The tool returns structured JSON plus "
        "readable Chinese Markdown and never scans, sends, or changes delivery state."
    ),
    requires=("daily_decision_brief_state",),
    capabilities=("daily_brief", "decision_support", "read_only", "runtime_artifacts"),
    input_schema={
        "account": {
            "type": "string",
            "minLength": 1,
            "description": "Optional lowercase account label; omitted queries all enabled accounts",
        },
        "market": {
            "type": "string",
            "enum": ["US", "HK", "us", "hk"],
            "description": "Optional market; omitted latest queries all enabled markets and day queries default to US",
        },
        "date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "Optional market trading date; requires account; market defaults to US",
        },
        "revision": {
            "type": "integer",
            "minimum": 0,
            "description": "Optional exact revision; requires date and account; market defaults to US",
        },
    },
    handler=_daily_brief_read_tool,
    pure_read=True,
    safe_default_input={},
    input_validator=_validate_daily_brief_input,
    examples=(
        {"input": {}},
        {"input": {"market": "HK"}},
        {"input": {"account": "lx"}},
        {"input": {"account": "lx", "market": "US", "date": "2026-07-19", "revision": 0}},
    ),
    output_contract=_OUTPUT_CONTRACT,
    copilot_input_fields=("account", "market", "date", "revision"),
)

TOOLS: tuple[AgentTool, ...] = (DAILY_DECISION_BRIEF_READ_TOOL,)


__all__ = ["DAILY_DECISION_BRIEF_READ_TOOL", "TOOLS", "read_daily_brief_view"]
