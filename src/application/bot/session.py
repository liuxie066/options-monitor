"""Stable Host channel identity, retained across the Python runtime transition."""
import hashlib


from src.application.conversation_scope import derive_session_id


def session_key_for_contract(contract):
    from pathlib import Path
    from src.application.bot.memory import scope_from_contract
    scope = scope_from_contract(contract)
    data = contract.input
    authority = data.get("authority_scope")
    if not authority:
        authority = ("path:" + hashlib.sha256(str(Path(data["config_path"]).resolve(strict=True)).encode()).hexdigest()
                     if data.get("config_path") else "key:" + str(data.get("config_key") or ""))
    if scope.read_generation:
        authority = str(authority) + "|" + scope.read_generation
    return derive_session_id(str(data.get("authenticated_channel") or "").lower(),
                             str(data.get("authenticated_sender_id") or ""),
                             str(data.get("authenticated_conversation_id") or ("sender:" + str(data.get("authenticated_sender_id") or ""))), authority)
