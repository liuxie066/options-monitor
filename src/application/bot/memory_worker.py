"""Authenticated scope and verified sources for explicit personal memory."""
from typing import Any
import json
from src.application.bot.contracts import contract_from_payload
from src.application.bot.memory import scope_from_contract, _json
from src.application.research.redaction import redact_value

def configured_memory_scope(contract: Any) -> Any:
    """Reuse the canonical config identity gate and its authoritative account names."""
    from src.application.account_config import accounts_from_config
    from src.application.agent_tool_config import load_runtime_config

    scope_from_contract(contract)  # Reject untrusted/local callers before loading configuration.
    _, config = load_runtime_config(config_key=contract.input.get("config_key"),
                                    config_path=contract.input.get("config_path"))
    try:
        raw_accounts = config.get("accounts")
        # Older generated snapshots used an account->settings mapping; the
        # current canonical shape is a validated list of labels.
        accounts = list(raw_accounts) if isinstance(raw_accounts, dict) else accounts_from_config(config, fallback=())
    except (TypeError, ValueError):
        raise ValueError("MEMORY_ACCOUNT_CONFIG_INVALID")
    return scope_from_contract(contract, accounts)

def verified_sources_from_run(run: dict, *, scope: Any = None) -> dict:
    """Recover original admitted tool observations, never assistant text or summaries."""
    from src.application.agent_tool_registry import get_tool_definition

    contract = contract_from_payload(json.loads(run["contract_json"]))
    scope = scope or configured_memory_scope(contract)
    if scope.owner_scope != scope_from_contract(contract).owner_scope:
        raise ValueError("MEMORY_OWNER_MISMATCH")
    sources = {}
    for event in json.loads(run["events_json"]):
        observation = event.get("payload") or {}
        if event.get("type") != "tool_result" or observation.get("ok") is not True:
            continue
        name = observation.get("tool_name")
        definition = get_tool_definition(name) if isinstance(name, str) else None
        if definition is None or not definition.enabled or not definition.is_pure_read():
            continue
        value = observation.get("data", observation.get("value"))
        if not isinstance(value, (dict, list)) or not observation.get("ref"):
            continue
        metadata = value if "data" in observation and isinstance(value, dict) else observation
        if (metadata.get("coverage", {}).get("status") != "complete"
            or metadata.get("missing_data") or metadata.get("warnings")):
            continue
        if "data" not in observation and (observation.get("status") not in {"complete", "not_found"}
                                          or not observation.get("content_hash")):
            continue
        accounts: set[str] = set()
        def collect_accounts(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in {"account", "account_scope"} and isinstance(child, str) and child:
                        accounts.add(child)
                    else:
                        collect_accounts(child)
            elif isinstance(item, list):
                for child in item:
                    collect_accounts(child)
        collect_accounts(value)
        collect_accounts(metadata.get("coverage", {}).get("scope"))
        collect_accounts(observation.get("tool_input"))
        if len(accounts) != 1 or not accounts <= scope.allowed_accounts:
            continue
        account = next(iter(accounts))
        source_time = metadata.get("as_of") or metadata.get("freshness", {}).get("as_of") or event.get("timestamp")
        if not isinstance(source_time, str) or not source_time:
            continue
        source = {"kind": "evidence", "account_scope": account,
                  "text": _json(value), "source_time": source_time}
        if redact_value(source) != source:
            continue
        sources[f"evidence:{run['run_id']}:{observation['ref']}"] = source
    return sources
