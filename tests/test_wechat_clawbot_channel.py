from __future__ import annotations

import json
from pathlib import Path


def test_wechat_clawbot_qrcode_writes_pending_login(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import start_wechat_clawbot_qrcode

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, bot_token, base_url: str, timeout: int) -> None:  # type: ignore[no-untyped-def]
            captured["bot_token"] = bot_token
            captured["base_url"] = base_url
            captured["timeout"] = timeout

        def get_bot_qrcode(self, *, bot_type: int):  # type: ignore[no-untyped-def]
            captured["bot_type"] = bot_type
            return {"data": {"qrcode": "qr_1"}}

    out = start_wechat_clawbot_qrcode(
        base=tmp_path,
        label="ops",
        state_dir=str(tmp_path / "wechat-state"),
        base_url="https://example.invalid",
        timeout_sec=7,
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["data"]["qrcode"] == "qr_1"
    assert captured == {"bot_token": None, "base_url": "https://example.invalid", "timeout": 7, "bot_type": 3}
    pending = json.loads((tmp_path / "wechat-state" / "pending_login.json").read_text(encoding="utf-8"))
    assert pending["qrcode"] == "qr_1"


def test_wechat_clawbot_qr_status_persists_bot_token(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import check_wechat_clawbot_qrcode

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "pending_login.json").write_text(
        json.dumps({"qrcode": "qr_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token, base_url: str, timeout: int) -> None:  # type: ignore[no-untyped-def]
            assert bot_token is None
            assert base_url == "https://example.invalid"

        def get_qrcode_status(self, *, qrcode: str):  # type: ignore[no-untyped-def]
            assert qrcode == "qr_1"
            return {"data": {"status": "confirmed", "bot_token": "bot_1", "get_updates_buf": "buf_1"}}

    out = check_wechat_clawbot_qrcode(
        base=tmp_path,
        label="ops",
        state_dir=str(state_dir),
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["data"]["bound"] is True
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["bot_token"] == "bot_1"
    assert state["get_updates_buf"] == "buf_1"


def test_wechat_clawbot_bind_persists_context_token(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import bind_wechat_clawbot_target

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            assert bot_token == "bot_1"
            assert base_url == "https://example.invalid"

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert get_updates_buf == "buf_1"
            return {
                "data": {
                    "get_updates_buf": "buf_2",
                    "message_list": [
                        {
                            "from_user_id": "user_1",
                            "group_id": "group_1",
                            "context_token": "ctx_1",
                            "message_id": "msg_1",
                            "text_item": {"text": "bind ops"},
                        }
                    ],
                }
            }

    out = bind_wechat_clawbot_target(
        base=tmp_path,
        label="ops",
        name="prod",
        match_text="bind ops",
        state_dir=str(state_dir),
        client_factory=FakeClient,
    )

    assert out["ok"] is True
    assert out["data"]["target"] == "wechat:ops:prod"
    bindings = json.loads((state_dir / "bindings.json").read_text(encoding="utf-8"))["bindings"]
    assert bindings["prod"]["to_user_id"] == "user_1"
    assert bindings["prod"]["context_token"] == "ctx_1"
    assert bindings["prod"]["group_id"] == "group_1"
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["get_updates_buf"] == "buf_2"


def test_wechat_clawbot_bind_failure_does_not_advance_cursor(tmp_path: Path) -> None:
    from src.application.channels.wechat_clawbot.binding import bind_wechat_clawbot_target

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_1", "base_url": "https://example.invalid", "get_updates_buf": "buf_1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *, bot_token: str, base_url: str, timeout: int) -> None:
            assert bot_token == "bot_1"

        def get_updates(self, *, get_updates_buf: str):  # type: ignore[no-untyped-def]
            assert get_updates_buf == "buf_1"
            return {
                "data": {
                    "get_updates_buf": "buf_2",
                    "message_list": [
                        {
                            "from_user_id": "user_1",
                            "context_token": "ctx_1",
                            "message_id": "msg_1",
                            "text_item": {"text": "bind ops"},
                        }
                    ],
                }
            }

    out = bind_wechat_clawbot_target(
        base=tmp_path,
        label="ops",
        name="prod",
        match_text="missing text",
        state_dir=str(state_dir),
        client_factory=FakeClient,
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "BINDING_MESSAGE_NOT_FOUND"
    assert out["data"]["candidate_count"] == 1
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert state["get_updates_buf"] == "buf_1"
    assert not (state_dir / "bindings.json").exists()


def test_cli_channel_wechat_clawbot_list_reads_local_state(tmp_path: Path, capsys) -> None:
    import src.interfaces.cli.main as cli

    state_dir = tmp_path / "wechat-state"
    state_dir.mkdir()
    (state_dir / "bindings.json").write_text(
        json.dumps({"bindings": {"ops": {"to_user_id": "user_1", "context_token": "ctx_1"}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    rc = cli.main(["channel", "wechat-clawbot", "list", "--state-dir", str(state_dir)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"]["binding_count"] == 1
    assert "context_token" not in payload["data"]["bindings"]["ops"]
