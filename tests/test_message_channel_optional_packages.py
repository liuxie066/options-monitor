"""Installed-package and absent-package checks run in fresh, offline processes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("om_feishu_channel", "om_wechat_channel")


def _run(code: str, *, blocked: tuple[str, ...] = ()) -> None:
    prelude = f'''
import importlib.util
import socket
import sys
import urllib.request
from importlib.abc import MetaPathFinder
blocked = {blocked!r}
original_find_spec = importlib.util.find_spec
def find_spec(name, *args, **kwargs):
    if any(name == root or name.startswith(root + ".") for root in blocked):
        return None
    return original_find_spec(name, *args, **kwargs)
importlib.util.find_spec = find_spec
class AbsentChannel(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == root or fullname.startswith(root + ".") for root in blocked):
            raise ModuleNotFoundError("channel intentionally absent", name=fullname)
sys.meta_path.insert(0, AbsentChannel())
network_calls = []
def forbidden_network(*args, **kwargs):
    network_calls.append((args, kwargs))
    raise AssertionError("optional-package check attempted network access")
socket.socket.connect = forbidden_network
urllib.request.urlopen = forbidden_network
'''
    result = subprocess.run(
        [sys.executable, "-c", prelude + code + "\nassert network_calls == [], network_calls\n"],
        cwd=ROOT, capture_output=True,
        text=True, timeout=60,
        env={**os.environ, "OM_SECRET_BACKEND": "env", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("blocked", [(), PACKAGES, (PACKAGES[0],), (PACKAGES[1],)])
@pytest.mark.parametrize("entry,args", [
    ("src.interfaces.cli.main", ["help"]),
    ("src.interfaces.agent.cli", ["spec"]),
])
def test_public_startup_never_loads_channel_packages(blocked, entry, args):
    _run(f'''
from importlib import import_module
assert import_module({entry!r}).main({args!r}) == 0
assert not any(name in sys.modules for name in {PACKAGES!r})
assert "lark_oapi" not in sys.modules
''', blocked=blocked)


def test_installed_distributions_have_isolated_wheels_and_lazy_facades():
    _run('''
from importlib.metadata import distribution
for name, module in (("om-feishu-channel-plugin", "om_feishu_channel"),
                     ("om-wechat-channel-plugin", "om_wechat_channel")):
    dist = distribution(name)
    assert dist.version == "0.1.0"
    assert any(str(path).startswith(module + "/") for path in dist.files)
    assert not any(str(path).startswith(("src/", "domain/")) for path in dist.files)
    assert module not in sys.modules
feishu = distribution("om-feishu-channel-plugin")
assert any(req.split(";")[0].strip() == "lark-oapi" for req in feishu.requires)
assert not distribution("om-wechat-channel-plugin").requires
from src.infrastructure.feishu_bot import send_message, FEISHU_SEND_TOO_LARGE
from src.infrastructure.feishu_ws_client import start_feishu_ws_client
from src.application.channels.wechat_clawbot.ilink_client import (
    DEFAULT_ILINK_BASE_URL, WechatClawbotClient, WechatClawbotError,
)
assert callable(send_message) and callable(start_feishu_ws_client)
assert callable(WechatClawbotClient) and issubclass(WechatClawbotError, RuntimeError)
assert DEFAULT_ILINK_BASE_URL and FEISHU_SEND_TOO_LARGE
assert "om_feishu_channel" not in sys.modules
assert "om_wechat_channel" not in sys.modules
''')


@pytest.mark.parametrize("channel,distribution,call", [
    ("feishu", "om-feishu-channel-plugin", "send_text_message(app_id='fixture', app_secret='fixture', open_id='fixture', text='fixture')"),
    ("feishu", "om-feishu-channel-plugin", "start_feishu_ws_client(app_id='fixture', app_secret='fixture', on_event=lambda event: None)"),
    ("wechat_clawbot", "om-wechat-channel-plugin", "WechatClawbotClient(bot_token='fixture')"),
    ("wechat_clawbot", "om-wechat-channel-plugin", "http_json('POST', 'https://fixture.invalid')"),
])
def test_absent_transport_fails_explicitly_before_network(channel, distribution, call):
    _run(f'''
from src.application.agent_tool_contracts import AgentToolError
from src.infrastructure.feishu_bot import send_text_message
from src.infrastructure.feishu_ws_client import start_feishu_ws_client, is_feishu_ws_sdk_available
from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient, http_json
assert is_feishu_ws_sdk_available() is False
try:
    {call}
except AgentToolError as exc:
    assert exc.code == "CHANNEL_PLUGIN_NOT_INSTALLED"
    assert {distribution!r} in str(exc)
else:
    raise AssertionError("absent transport was accepted")
assert "om_feishu_channel" not in sys.modules
assert "om_wechat_channel" not in sys.modules
''', blocked=PACKAGES)


@pytest.mark.parametrize("blocked", [(), PACKAGES, (PACKAGES[0],), (PACKAGES[1],)])
def test_transports_preserve_core_report_history_and_data_owners(blocked):
    _run('''
from pathlib import Path
from tempfile import TemporaryDirectory
from src.application import pipeline_runtime, portfolio_context_builder
from src.application.channels.feishu_notification_renderer import render_feishu_notification_card
from src.application.daily_decision_brief_repository import (
    persist_daily_decision_brief_success, prepare_daily_decision_brief_delivery,
    read_daily_decision_brief_delivery_state, read_latest_daily_decision_brief,
    query_daily_brief_receipts,
)
from src.infrastructure import feishu_bitable
from tests.test_daily_decision_brief_repository import _brief
message = "隔离报告正文"
transport = render_feishu_notification_card(markdown=message, fallback_text=message, fallback_open_id="fixture")
with TemporaryDirectory() as directory:
    base = Path(directory)
    persisted = persist_daily_decision_brief_success(base=base, brief=_brief(run_id="fixture-run"))
    prepared = prepare_daily_decision_brief_delivery(
        base=base, account="lx", market="US", market_trading_date="2026-07-17",
        run_id="fixture-run", delivery_kind="fixed_report", source_kind="successful_brief",
        source_digest=persisted["current_brief_digest"], revision=persisted["current_revision"],
        scheduled_target_market="2026-07-17T10:00:00-04:00",
        rendered_message=message, rendered_transport=transport,
    )
    assert read_latest_daily_decision_brief(base=base, account="lx", market="US")["available"]
    state = read_daily_decision_brief_delivery_state(base=base, account="lx", market="US")
    assert state["available"]
    assert message in str(state["state"])
    assert query_daily_brief_receipts(base=base, account="lx", market="US", query={})
assert portfolio_context_builder.FeishuAuthError is feishu_bitable.FeishuAuthError
assert portfolio_context_builder.safe_float is feishu_bitable.safe_float
assert feishu_bitable.safe_float("12.5") == 12.5
assert "om_feishu_channel" not in sys.modules
assert "om_wechat_channel" not in sys.modules
assert "lark_oapi" not in sys.modules
''', blocked=blocked)


def test_sdk_presence_without_message_plugin_is_unavailable():
    _run('''
from src.infrastructure.feishu_ws_client import is_feishu_ws_sdk_available
assert original_find_spec("lark_oapi") is not None
assert is_feishu_ws_sdk_available() is False
assert "lark_oapi" not in sys.modules
''', blocked=(PACKAGES[0],))


@pytest.mark.parametrize("channel", ["feishu", "wechat_clawbot"])
def test_selected_transport_does_not_require_other_channel(channel):
    if channel == "feishu":
        code = '''
from src.infrastructure import feishu_bot
feishu_bot.with_tenant_token_retry = lambda app, secret, send: send("fixture-token")
assert feishu_bot.send_text_message(
    app_id="fixture", app_secret="fixture", open_id="fixture", text="fixture",
    http_json_fn=lambda *args, **kwargs: {"code": 0},
) == {"code": 0}
assert "om_feishu_channel.bot" in sys.modules
assert "om_wechat_channel" not in sys.modules
'''
        blocked = (PACKAGES[1],)
    else:
        code = '''
from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient
client = WechatClawbotClient(bot_token="fixture", http_json_fn=lambda *args, **kwargs: {"ret": 0})
assert client.send_text_message(to_user_id="fixture", context_token="fixture", text="fixture") == {"ret": 0}
assert "om_wechat_channel.ilink_client" in sys.modules
assert "om_feishu_channel" not in sys.modules
'''
        blocked = (PACKAGES[0],)
    _run(code, blocked=blocked)


def test_installed_feishu_without_sdk_reports_missing_connector_dependency():
    _run('''
from src.infrastructure.feishu_ws_client import is_feishu_ws_sdk_available, start_feishu_ws_client
assert is_feishu_ws_sdk_available() is False
try:
    start_feishu_ws_client(app_id="fixture", app_secret="fixture", on_event=lambda payload: None)
except RuntimeError as exc:
    assert "lark-oapi is required" in str(exc)
else:
    raise AssertionError("missing SDK was accepted")
''', blocked=("lark_oapi",))
