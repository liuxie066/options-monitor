"""Explicit bot/API entry points; each process owns one instance lock."""
from __future__ import annotations

from pathlib import Path
import fcntl
import os


def _instance_lock(data_dir: Path, name: str):
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(data_dir / f"{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise RuntimeError("group-query instance is already running") from exc
    return descriptor


def _release_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def run_preflight(config_path, *, confirm: bool = False, **live_dependencies) -> dict:
    from .config import load_preflight_settings

    settings = load_preflight_settings(config_path)
    if not confirm:
        return {"status": "live_not_run", "config_valid": True,
                "config_sha256": settings.public_digest(), "live_g2_verified": False}
    from .preflight import run_live
    return run_live(settings, **live_dependencies)


def run_serve(config_path, *, secret_provider=None, start_ws_fn=None,
              reply_fn=None, bot_open_id: str | None = None,
              request=None) -> dict:
    """Bot process: Feishu transport, model turns and delivery state only."""
    from .config import load_bot_settings

    settings = load_bot_settings(config_path)
    descriptor = _instance_lock(settings.data_dir, "bot")
    try:
        from .agent import BotService, BusinessWorker
        from .feishu import resolve_bot_identity

        kwargs = {"secret_provider": secret_provider, "reply_fn": reply_fn}
        if request is not None:
            kwargs["request"] = request
        service = BotService(settings, **kwargs)
        identity = bot_open_id or resolve_bot_identity(settings, service.feishu_secret)
        worker = BusinessWorker(service, identity)
        if start_ws_fn is None:
            from src.infrastructure.feishu_ws_client import start_feishu_ws_client
            start_ws_fn = start_feishu_ws_client
        try:
            start_ws_fn(app_id=settings.app_id, app_secret=service.feishu_secret,
                        on_event=worker.submit)
        finally:
            worker.close()
    finally:
        _release_lock(descriptor)
    return {"status": "stopped", "instance_id": settings.instance_id,
            "live_g2_verified": False}


def run_api_serve(config_path, *, secret_provider=None, dependencies=None) -> dict:
    """API process: token, public quote/config and account-free business only."""
    from .config import load_api_settings

    settings = load_api_settings(config_path)
    descriptor = _instance_lock(settings.data_dir, "api")
    try:
        from .api import resolve_api_token, serve
        from .store import DeliveryStore

        serve(settings, token=resolve_api_token(settings, provider=secret_provider),
              store=DeliveryStore(settings.data_dir), dependencies=dependencies)
    finally:
        _release_lock(descriptor)
    return {"status": "stopped", "instance_id": settings.instance_id}


__all__ = ["run_api_serve", "run_preflight", "run_serve"]
