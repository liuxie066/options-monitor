from __future__ import annotations

import hashlib
import json
from base64 import b64encode
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import build_response
from src.application.inbound.feishu_gateway import (
    FeishuGatewaySettings,
    build_feishu_gateway_settings,
    build_feishu_signature,
    decrypt_feishu_event_payload,
    handle_feishu_gateway_http,
    verify_feishu_signature,
)


def _body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _encrypted_payload(payload: dict, *, encrypt_key: str) -> dict:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = _body(payload)
    pad = 16 - (len(raw) % 16)
    padded = raw + bytes([pad]) * pad
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return {"encrypt": b64encode(encrypted).decode("ascii")}


def test_feishu_signature_verification_matches_official_contract() -> None:
    raw = _body({"event": {"message": {"content": "x"}}})
    signature = build_feishu_signature(
        timestamp="1000",
        nonce="nonce-1",
        encrypt_key="secret-key",
        raw_body=raw,
    )

    verify_feishu_signature(
        raw_body=raw,
        headers={
            "X-Lark-Request-Timestamp": "1000",
            "X-Lark-Request-Nonce": "nonce-1",
            "X-Lark-Signature": signature,
        },
        encrypt_key="secret-key",
        now_fn=lambda: 1000.0,
    )


def test_feishu_signature_verification_rejects_bad_signature() -> None:
    raw = _body({"event": {"message": {"content": "x"}}})

    status, payload = handle_feishu_gateway_http(
        raw_body=raw,
        headers={
            "X-Lark-Request-Timestamp": "1000",
            "X-Lark-Request-Nonce": "nonce-1",
            "X-Lark-Signature": "bad",
        },
        settings=FeishuGatewaySettings(encrypt_key="secret-key", require_signature=True, reply_enabled=False),
        now_fn=lambda: 1000.0,
    )

    assert status == 401
    assert payload["error"]["code"] == "PERMISSION_DENIED"


def test_feishu_gateway_returns_url_verification_challenge() -> None:
    status, payload = handle_feishu_gateway_http(
        raw_body=_body({"type": "url_verification", "token": "verify-token", "challenge": "challenge-token"}),
        headers={},
        settings=FeishuGatewaySettings(verification_token="verify-token", require_signature=True),
    )

    assert status == 200
    assert payload == {"challenge": "challenge-token"}


def test_feishu_gateway_decrypts_encrypted_payload() -> None:
    decrypted = decrypt_feishu_event_payload(
        _encrypted_payload({"type": "url_verification", "challenge": "challenge-token"}, encrypt_key="secret-key"),
        "secret-key",
    )

    assert decrypted == {"type": "url_verification", "challenge": "challenge-token"}


def test_feishu_gateway_delegates_to_inbound_and_replies(tmp_path: Path) -> None:
    raw_payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1", "token": "verify-token"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "msg_1",
                "message_type": "text",
                "content": json.dumps({"text": "收益 sy 2026-05"}, ensure_ascii=False),
            },
        },
    }
    replies: list[dict] = []
    calls: list[tuple[str, dict]] = []

    def _execute(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(
            tool_name=tool_name,
            ok=True,
            data={"summary": [{"month": "2026-05", "account": "sy", "currency": "HKD"}]},
        )

    def _reply(**kwargs) -> dict:  # type: ignore[no-untyped-def]
        replies.append(dict(kwargs))
        return {"code": 0, "data": {"message_id": "reply_1"}}

    status, payload = handle_feishu_gateway_http(
        raw_body=_body(raw_payload),
        headers={},
        settings=FeishuGatewaySettings(
            allowed_senders="feishu:ou_1",
            app_id="app_1",
            app_secret="secret_1",
            verification_token="verify-token",
            require_signature=False,
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
        execute_tool_fn=_execute,
    )

    assert status == 200
    assert payload["ok"] is True
    assert payload["data"]["reply"]["reason"] == "sent"
    assert calls == [("monthly_income_report", {"config_key": "us", "account": "sy", "month": "2026-05"})]
    assert replies[0]["message_id"] == "msg_1"
    assert replies[0]["text"].startswith("收益统计完成")


def test_feishu_gateway_does_not_reply_to_denied_sender(tmp_path: Path) -> None:
    raw_payload = {
        "schema": "2.0",
        "header": {"event_id": "evt_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_bad"}},
            "message": {
                "message_id": "msg_1",
                "message_type": "text",
                "content": json.dumps({"text": "状态"}, ensure_ascii=False),
            },
        },
    }
    replies: list[dict] = []

    def _reply(**kwargs) -> dict:  # type: ignore[no-untyped-def]
        replies.append(dict(kwargs))
        return {"code": 0}

    status, payload = handle_feishu_gateway_http(
        raw_body=_body(raw_payload),
        headers={},
        settings=FeishuGatewaySettings(
            allowed_senders="feishu:ou_good",
            app_id="app_1",
            app_secret="secret_1",
            require_signature=False,
            audit_db=str(tmp_path / "audit.sqlite3"),
        ),
        reply_fn=_reply,
    )

    assert status == 200
    assert payload["data"]["reply"]["reason"] == "permission_denied"
    assert replies == []


def test_feishu_gateway_settings_prefers_inbound_app_credentials() -> None:
    settings = build_feishu_gateway_settings(
        environ={
            "OM_INBOUND_FEISHU_APP_ID": "in_app",
            "OM_INBOUND_FEISHU_APP_SECRET": "in_secret",
            "OM_NOTIFY_FEISHU_APP_ID": "notify_app",
            "OM_NOTIFY_FEISHU_APP_SECRET": "notify_secret",
            "OM_INBOUND_FEISHU_ENCRYPT_KEY": "encrypt",
        }
    )

    assert settings.app_id == "in_app"
    assert settings.app_secret == "in_secret"
    assert settings.encrypt_key == "encrypt"
