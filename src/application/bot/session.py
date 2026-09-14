"""Stable Host channel identity, retained across the Python runtime transition."""
import hashlib


def derive_session_id(channel: str, sender: str, conversation: str, authority_scope: str) -> str:
    parts = (channel, sender, conversation, authority_scope)
    if any(not isinstance(part, str) or not part.strip() or "\0" in part for part in parts):
        raise ValueError("session identity parts must be nonempty and contain no NUL")
    # Existing Host leases and cancellations use this digest; no Pi database is opened.
    material = "om-pi-session-v1\0" + "\0".join(parts)
    return "om_" + hashlib.sha256(material.encode()).hexdigest()


def session_key_for_contract(contract):
    from pathlib import Path
    from src.application.bot.memory import scope_from_contract
    scope_from_contract(contract)
    data = contract.input
    authority = data.get("authority_scope")
    if not authority:
        authority = ("path:" + hashlib.sha256(str(Path(data["config_path"]).resolve(strict=True)).encode()).hexdigest()
                     if data.get("config_path") else "key:" + str(data.get("config_key") or ""))
    return derive_session_id(str(data.get("authenticated_channel") or "").lower(),
                             str(data.get("authenticated_sender_id") or ""),
                             str(data.get("authenticated_conversation_id") or ("sender:" + str(data.get("authenticated_sender_id") or ""))), authority)
