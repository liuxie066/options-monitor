"""Public preview/confirm facade for withdrawal reservation corrections."""
from src.application.agent_tools.base import build_agent_tool
from src.application.agent_tool_config import load_runtime_config
from src.application.withdrawal_correction import correction


def handle(payload):
    from src.application.futu_portfolio_context import infer_futu_portfolio_settings
    from src.application.account_config import resolve_futu_account_ids
    from src.infrastructure.futu_gateway import build_ready_futu_broker_gateway

    _, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    account = payload["account"]
    if payload.get("confirm") and (not payload.get("expected_hash") or not payload.get("operation_id")):
        raise ValueError("confirmation requires preview hash and operation ID")
    settings = infer_futu_portfolio_settings(cfg, account=account)
    ids = resolve_futu_account_ids(cfg, account=account)
    if len(ids) != 1:
        raise ValueError("exactly one broker account required")
    environment = settings.get("trd_env", "REAL")
    gateway = build_ready_futu_broker_gateway(host=settings["host"], port=int(settings["port"]),
                                             expected_account_ids=set(ids), trd_env=environment,
                                             is_option_chain_cache_enabled=False)
    try:
        result = correction(cfg=cfg, account=account, account_id=next(iter(ids)), environment=environment,
                            gateway=gateway, record_id=payload["record_id"], action=payload["action"],
                            flow_ids=payload.get("flow_ids", []), reason=payload["reason"], actor=payload["actor"],
                            expected_hash=payload.get("expected_hash") if payload.get("confirm") else None,
                            operation_id=payload.get("operation_id") if payload.get("confirm") else None)
    finally:
        gateway.close()
    return result, [], {}


TOOLS = (build_agent_tool(
    name="withdrawal_correct", description="Preview or confirm a withdrawal reservation correction; never writes broker balances or PM records.",
    catalog_summary="预览或确认出金平账、恢复预扣、取消出金；保留订正证据。",
    requires=("runtime_config", "pm", "futu"), capabilities=("withdrawal_reserve",),
    input_schema={
        "config_key": "optional us or hk", "config_path": "optional runtime config path",
        "account": {"type": "string", "required": True},
        "record_id": {"type": "string", "required": True},
        "action": {"type": "string", "required": True, "enum": ["settle", "restore", "cancel"]},
        "flow_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string", "required": True},
        "actor": {"type": "string", "required": True},
        "expected_hash": "optional preview hash, required on confirm",
        "operation_id": "optional operation ID, required on confirm", "confirm": "optional boolean",
    }, handler=handle, side_effects=("withdrawal_reserve_write",), requires_confirm=True,
    write_request_predicate=lambda payload: bool(payload.get("confirm")),
    safe_default_input={"confirm": False}, allow_additional_input=False,
),)
