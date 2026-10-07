"""Authorized decision history read surface."""
import hashlib
import hmac
from time import monotonic

from src.application.account_config import accounts_from_config, build_account_runtime_plan
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import build_agent_tool
from src.application.agent_tools.project import _QUERY_CONTEXT
from src.application.decision_history import read_decision_history
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.runtime_paths import resolve_runtime_root
from src.application.secret_store import INBOUND_OPERATION_HMAC_KEY, SecretError, resolve_secret
from src.infrastructure.decision_history_sqlite import content_hash


def _key():
    try:
        key = resolve_secret(INBOUND_OPERATION_HMAC_KEY)
    except SecretError as exc:
        raise AgentToolError(code="DEPENDENCY_MISSING", message="历史查询翻页签名密钥不可用。") from exc
    if not key:
        raise AgentToolError(code="DEPENDENCY_MISSING", message="历史查询翻页签名密钥不可用。")
    return hmac.new(key.encode(), b"options-monitor/decision-history/v1", hashlib.sha256).hexdigest()


def _read(payload):
    path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    accounts = accounts_from_config(cfg, fallback=())
    account = str(payload.get("account") or "").lower()
    market = str(payload.get("market") or "").upper()
    if account not in accounts or market.lower() != infer_runtime_config_market(config=cfg, config_key=payload.get("config_key"), config_path=path):
        raise AgentToolError(code="SCOPE_DENIED", message="账户或市场不在当前授权范围。")
    binding = build_account_runtime_plan(cfg, account=account)
    scope = {"futu_account_id": binding.futu_account_id, "trade_env": binding.futu_trd_env}
    for field in scope:
        if payload.get(field) and payload[field] != scope[field]:
            raise AgentToolError(code="SCOPE_DENIED", message="物理账户或交易环境不在当前授权范围。")
    if not scope["futu_account_id"] or scope["trade_env"] not in {"REAL", "SIMULATE"}:
        raise AgentToolError(code="SCOPE_UNPROVEN", message="无法确认物理账户和实盘/模拟身份，请补充有效账户配置。")
    authority = content_hash({"config": str(path.resolve()), "scope": scope, "identity": {k: payload.get(k) for k in
        ("authenticated_channel", "authenticated_sender_id", "authenticated_conversation_id", "authority_scope")}})
    deadline, cancelled = _QUERY_CONTEXT.get()
    base = resolve_runtime_root(repo_root=repo_base()).runtime_root
    try:
        result = read_decision_history(base=base, account=account, market=market, scope=scope,
            start_date=payload["start_date"], end_date=payload["end_date"], symbol=payload.get("symbol"),
            cursor=payload.get("cursor"), limit=payload.get("limit", 10), reference=payload.get("reference"),
            authority=authority, cursor_key=_key,
            cancelled=lambda: (deadline is not None and monotonic() >= deadline) or (cancelled is not None and cancelled()))
    except (ValueError, KeyError) as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    if result["rows"]:
        from src.application.decision_history_results import attach_trade_results
        from src.application.ledger.api import resolve_position_ledger_sqlite_path
        attach_trade_results(result, ledger_path=resolve_position_ledger_sqlite_path(base=base, cfg=cfg, config_path=path, runtime_root=base), scope=scope, account=account)
    return result, [], {"read_only": True}


_FIELDS = ("account", "market", "start_date", "end_date", "symbol", "futu_account_id", "trade_env", "cursor")
_INPUT = {field: {"type": "string", "minLength": 1, "maxLength": 8192 if field == "cursor" else 128} for field in _FIELDS}
for _field in ("account", "market", "start_date", "end_date"):
    _INPUT[_field]["required"] = True
_INPUT.update({"limit": {"type": "integer", "minimum": 1, "maximum": 50}, "reference": {"type": "object"},
    "config_key": {"type": "string", "enum": ["us", "hk"]}, "config_path": {"type": "string"},
    **{k: {"type": "string"} for k in ("authenticated_channel", "authenticated_sender_id", "authenticated_conversation_id", "authority_scope")}})
TOOLS = (build_agent_tool(name="decision_history_read", description="Read immutable SQLite decisions and explicitly linked ledger results; never scans, sends or imports. Keep selectors unchanged when paging.",
    catalog_summary="查询当时的建议、历史通知原始版本及明确关联交易结果；缺失关联不代表没交易。",
    requires=("decision_history_sqlite",), capabilities=("read_only", "decision_history"), pure_read=True,
    input_schema=_INPUT, handler=_read, allow_additional_input=False, bot_input_fields=(*_FIELDS, "limit", "reference"),
    output_contract={"schema_version": "decision_history_read.output.v1", "evidence_type": "collection",
        "bounded_projection": "contract_fields", "coverage": "source_declared", "freshness": "source_declared",
        "primary_rows": "rows", "pagination": {"mode": "keyset"},
        "fact_fields": ["status", "rows", "missing", "next_cursor"],
        "model_preview_fields": ["status", "rows", "missing", "next_cursor", "coverage", "freshness"],
        "freshness_fields": ["freshness.as_of"], "missing_data_fields": ["missing", "rows.trade_results"]}),)
