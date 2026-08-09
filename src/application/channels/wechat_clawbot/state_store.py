from __future__ import annotations

import base64
import html
import json
import mimetypes
from pathlib import Path
from typing import Any

from src.infrastructure.private_storage import (
    atomic_write_private_bytes,
    atomic_write_private_text,
    ensure_private_directory,
    private_path,
)


class WechatClawbotStateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = private_path(state_dir)
        self.state_path = self.state_dir / "state.json"
        self.pending_login_path = self.state_dir / "pending_login.json"
        self.bindings_path = self.state_dir / "bindings.json"
        self.outbound_receipts_path = self.state_dir / "outbound_receipts.json"

    def load_state(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._read_json_object(self.state_path, default=default)

    def save_state(self, payload: dict[str, Any]) -> None:
        self._write_json_object(self.state_path, payload)

    def load_pending_login(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._read_json_object(self.pending_login_path, default=default)

    def save_pending_login(self, payload: dict[str, Any]) -> None:
        self._write_json_object(self.pending_login_path, payload)

    def load_bindings(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._read_json_object(self.bindings_path, default=default)

    def save_bindings(self, payload: dict[str, Any]) -> None:
        self._write_json_object(self.bindings_path, payload)

    def load_outbound_receipts(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._read_json_object(self.outbound_receipts_path, default=default)

    def save_outbound_receipts(self, payload: dict[str, Any]) -> None:
        self._write_json_object(self.outbound_receipts_path, payload)

    def write_qrcode_artifact(self, content: str | None) -> str | None:
        raw = str(content or "").strip()
        if not raw:
            return None
        ensure_private_directory(self.state_dir)
        if _looks_like_http_url(raw):
            target = self.state_dir / "login_qrcode.html"
            escaped_url = html.escape(raw, quote=True)
            atomic_write_private_text(
                target,
                (
                    "<!doctype html>\n"
                    "<html lang=\"zh-CN\">\n"
                    "<head>\n"
                    "  <meta charset=\"utf-8\">\n"
                    f"  <meta http-equiv=\"refresh\" content=\"0; url={escaped_url}\">\n"
                    "  <title>WeChat Login QR</title>\n"
                    "</head>\n"
                    "<body>\n"
                    "  <p>如果没有自动跳转，请打开下面的链接查看微信登录二维码：</p>\n"
                    f"  <p><a href=\"{escaped_url}\">{escaped_url}</a></p>\n"
                    "</body>\n"
                    "</html>\n"
                ),
            )
            return str(target)
        mime_type, payload = _extract_base64_payload(raw)
        try:
            binary = base64.b64decode(payload, validate=True)
        except Exception:
            target = self.state_dir / "login_qrcode.txt"
            atomic_write_private_text(target, raw)
            return str(target)
        extension = mimetypes.guess_extension(mime_type or "") if mime_type else None
        target = self.state_dir / f"login_qrcode{extension or _guess_binary_extension(binary)}"
        atomic_write_private_bytes(target, binary)
        return str(target)

    def safe_bindings(self) -> dict[str, dict[str, Any]]:
        payload = self.load_bindings(default={"bindings": {}})
        bindings = payload.get("bindings") if isinstance(payload.get("bindings"), dict) else {}
        out: dict[str, dict[str, Any]] = {}
        for name, value in bindings.items():
            if not isinstance(value, dict):
                continue
            out[str(name)] = {k: v for k, v in value.items() if k != "context_token"}
        return out

    def _read_json_object(self, path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return dict(default or {})
        except Exception as exc:
            raise ValueError(f"failed to read {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path.name} must be a JSON object")
        return payload

    def _write_json_object(self, path: Path, payload: dict[str, Any]) -> None:
        atomic_write_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n")


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _looks_like_http_url(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized.startswith("http://") or normalized.startswith("https://")


def _extract_base64_payload(content: str) -> tuple[str | None, str]:
    stripped = str(content or "").strip()
    if stripped.startswith("data:") and ";base64," in stripped:
        header, payload = stripped.split(",", 1)
        return header[5:].split(";", 1)[0].strip() or None, payload
    return None, stripped


def _guess_binary_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.lstrip().startswith(b"<svg"):
        return ".svg"
    return ".bin"
