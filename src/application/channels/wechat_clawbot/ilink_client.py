"""Stable iLink imports and lazy construction of the optional WeChat transport."""
from __future__ import annotations

from time import monotonic
from typing import Any, Callable

from src.application.channels.plugins import load_channel_plugin
from src.infrastructure.fetch_control import _FetchControlTimeout, check_fetch_control


DEFAULT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
HttpJsonFn = Callable[..., dict[str, Any]]
ILINK_CHANNEL_VERSION = "1.0.0"


class WechatClawbotError(RuntimeError):
    pass


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    check_fetch_control(deadline, cancelled)
    request_timeout = timeout if deadline is None else min(timeout, max(0.0, deadline - monotonic()))
    try:
        return load_channel_plugin("wechat_clawbot", module="ilink_client").http_json(
            method, url, payload, headers=headers, timeout=request_timeout,
        )
    except WechatClawbotError as exc:
        try:
            check_fetch_control(deadline, cancelled)
        except _FetchControlTimeout as stopped:
            raise stopped from exc
        raise


class WechatClawbotClient:
    """Construct the installed client without importing it during CLI registration."""

    def __new__(
        cls,
        *,
        bot_token: str | None = None,
        base_url: str = DEFAULT_ILINK_BASE_URL,
        timeout: int = 20,
        http_json_fn: HttpJsonFn = http_json,
    ) -> Any:
        return load_channel_plugin("wechat_clawbot", module="ilink_client").WechatClawbotClient(
            bot_token=bot_token, base_url=base_url, timeout=timeout, http_json_fn=http_json_fn,
        )
