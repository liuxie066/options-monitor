from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.bot.control import policy, upgrade_operations
from src.application.bot.control.contracts import BotInboundRequest
from src.application.bot.host_store import BotHostStore
from src.application.channels import plugins
from src.application.channels.feishu_reply_renderer import render_feishu_conversation_reply
from src.application.channels.wechat_clawbot import inbound as wechat
from src.application.inbound import feishu, feishu_ws


@pytest.fixture
def controls(tmp_path, monkeypatch):
    path = tmp_path / 'bot.json'
    monkeypatch.setattr('src.application.bot.control.config_loader.default_bot_config_path', lambda **kw: path)
    monkeypatch.setattr(plugins, 'channel_plugin_installed', lambda channel: True)

    def write(channel=None, **flags):
        cfg = {'bot': {'enabled': True}}
        if channel is not None:
            cfg['channels'] = {channel: {'enabled': True, 'receive_enabled': True, 'notify_enabled': False, **flags}}
        path.write_text(json.dumps(cfg))
        return str(path)

    write()
    return write


@pytest.mark.parametrize('channel', ['feishu', 'wechat_clawbot'])
@pytest.mark.parametrize('flags', [{'enabled': False}, {'receive_enabled': False}])
def test_current_controls_reloaded_and_other_channel_unaffected(controls, channel, flags):
    controls(channel)
    policy.require_inbound_channel(channel)
    controls(channel, **flags)
    with pytest.raises(AgentToolError, match='disabled') as blocked:
        policy.require_inbound_channel(channel)
    assert blocked.value.code == 'CHANNEL_RECEIVE_DISABLED'
    other = 'feishu' if channel == 'wechat_clawbot' else 'wechat_clawbot'
    controls(other)
    policy.require_inbound_channel(other)
    with pytest.raises(AgentToolError):
        policy.require_inbound_channel(channel)
    controls()
    policy.require_inbound_channel(channel)  # Legacy explicit invocation survives.


def test_absence_is_explicit_and_config_failure_fails_closed(controls, monkeypatch):
    monkeypatch.setattr(plugins, 'channel_plugin_installed', lambda channel: False)
    with pytest.raises(AgentToolError) as missing:
        policy.require_inbound_channel('feishu')
    assert missing.value.code == 'CHANNEL_PLUGIN_NOT_INSTALLED'
    monkeypatch.setattr(plugins, 'channel_plugin_installed', lambda channel: True)
    with pytest.raises(AgentToolError) as invalid:
        policy.require_inbound_channel('feishu', '/nonexistent/isolated-bot.json')
    assert invalid.value.code == 'CONFIG_ERROR'


@pytest.mark.parametrize('channel', ['feishu', 'wechat_clawbot'])
def test_disabled_public_entry_points_never_call_facades(controls, tmp_path, channel):
    path = controls(channel, enabled=False)
    forbidden = Mock(side_effect=AssertionError('disabled transport/business called'))
    if channel == 'feishu':
        result = feishu.handle_feishu_payload({}, bot_config_path=path, execute_tool_fn=forbidden)
        assert not result['ok']
        settings = feishu_ws.FeishuWsSettings(bot_config_path=path)
        result = feishu_ws.handle_feishu_ws_event({}, settings=settings, reply_fn=forbidden,
            reaction_fn=forbidden, channel_service=SimpleNamespace(handle_inbound=forbidden))
        assert result['error']['code'] == 'CHANNEL_RECEIVE_DISABLED'
        with pytest.raises(AgentToolError):
            feishu_ws.serve_feishu_ws(settings, start_client_fn=forbidden)
    else:
        result = wechat.handle_wechat_clawbot_message({}, bot_config_path=path, execute_tool_fn=forbidden)
        assert not result['ok']
        with pytest.raises(AgentToolError):
            wechat.poll_wechat_clawbot_once(base=tmp_path, bot_config_path=path, client_factory=forbidden,
                channel_service=SimpleNamespace(handle_inbound=forbidden))
    forbidden.assert_not_called()


@pytest.mark.parametrize('channel', ['feishu', 'wechat'])
@pytest.mark.parametrize('flag', ['enabled', 'receive_enabled'])
def test_reply_disable_after_claim_retains_retry_then_reenable_sends(controls, tmp_path, monkeypatch,
        example_config_path, channel, flag):
    from src.application.bot.config_scope import resolve_replay_authorization

    control_channel = 'wechat_clawbot' if channel == 'wechat' else channel
    controls(control_channel)
    db = str(tmp_path / 'replies.sqlite3')
    settings = feishu_ws.FeishuWsSettings(config_path=str(example_config_path),
        audit_db=db, allowed_senders='sender', app_id='app', app_secret='synthetic-secret')
    payload = (render_feishu_conversation_reply(message_id='incoming', text='reply', reply_in_thread=False,
        max_chars=3500, render_route='bot') if channel == 'feishu' else
        {'to_user_id': 'sender', 'context_token': 'ctx', 'group_id': None, 'text': 'reply'})
    authorization = resolve_replay_authorization(BotInboundRequest(text='', channel=channel, sender_id='sender',
        message_id='incoming', config_path=str(example_config_path),
        reply_context={'reply_sender_identity': 'app'}))
    store = BotHostStore(db)
    key = f'{channel}:pending'
    store.enqueue_reply(delivery_key=key, channel=channel, payload=payload, authorization=authorization)
    send = Mock(return_value={'code': 0, 'ret': 0, 'data': {'message_id': 'out'}})

    def retry():
        if channel == 'feishu':
            return feishu_ws._retry_pending_feishu_reply(settings=settings, reply_fn=send)
        return wechat._retry_pending_wechat_reply(audit_db=db, client=SimpleNamespace(send_text_message=send),
            config_path=str(example_config_path), transport_identity='app',
            allowed_senders_fn=lambda: 'sender')

    # Disabled before claim: no attempt budget or pending state is consumed.
    controls(control_channel, **{flag: False})
    assert retry()['attempted'] is False
    assert store.list_replies()[0]['attempt_count'] == 0
    controls(control_channel)
    policy.check_reply_authorization(store.list_replies()[0], channel=channel, current_allowed_senders=lambda: 'sender',
        config_key=None, config_path=str(example_config_path), bot_config_path=None, transport_identity='app')
    original_claim = BotHostStore.claim_reply

    def claim_then_disable(self, **kwargs):
        claimed = original_claim(self, **kwargs)
        if claimed and not claimed.get('authorization_blocked'):
            controls(control_channel, **{flag: False})
        return claimed

    monkeypatch.setattr(BotHostStore, 'claim_reply', claim_then_disable)
    result = retry()
    assert result['attempted'] is False
    assert result['reason'] == 'authorization_blocked'
    assert result['error_code'] == 'CHANNEL_RECEIVE_DISABLED'
    send.assert_not_called()
    row = store.list_replies()[0]
    assert row['status'] == 'retryable_failed'
    assert row['attempt_count'] == 1
    assert json.loads(row['payload_json']) == payload
    monkeypatch.setattr(BotHostStore, 'claim_reply', original_claim)
    controls(control_channel)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE bot_reply_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    assert retry()['ok'] is True
    assert send.call_count == 1
    assert store.list_replies()[0]['status'] == 'delivered'


def test_typing_second_call_and_stop_recheck_current_authority(controls):
    bot_path = controls('wechat_clawbot')
    def config(**kw):
        controls('wechat_clawbot', receive_enabled=False)
        return {'typing_ticket': 'ticket'}
    send = Mock()
    client = SimpleNamespace(get_config=Mock(side_effect=config), send_typing=send)
    message = {'from_user_id': 'sender', 'context_token': 'ctx'}
    result = wechat._maybe_start_typing(message=message, client=client, allowed_senders='sender',
        bot_config_path=bot_path)
    assert result['ok'] is False
    wechat._maybe_stop_typing(message=message, client=client,
        typing_status={'attempted': True, 'typing_ticket': 'ticket'}, bot_config_path=bot_path)
    assert client.get_config.call_count == 1
    send.assert_not_called()


def test_feishu_transport_guard_survives_token_refresh_boundary(controls):
    bot_path = controls('feishu')
    settings = feishu_ws.FeishuWsSettings(bot_config_path=bot_path)
    sent = Mock()
    def transport(**kwargs):
        controls('feishu', enabled=False)
        kwargs['before_send']()
        sent()
    envelope = render_feishu_conversation_reply(message_id='m', text='reply', reply_in_thread=False,
        max_chars=3500, render_route='bot')
    with pytest.raises(policy.ReplyAuthorizationError):
        feishu_ws._call_feishu_reply_transport(settings=settings, reply_fn=transport, envelope=envelope,
            transport=envelope['transport'], uuid='delivery')
    sent.assert_not_called()


def test_upgrade_final_retry_stays_in_original_channel_and_stops_on_disable(controls, monkeypatch):
    bot_path = controls('wechat_clawbot')
    send = Mock(side_effect=RuntimeError('explicit failure'))
    other = Mock(side_effect=AssertionError('cross channel fallback'))
    monkeypatch.setattr(upgrade_operations.time, 'sleep', lambda _: controls('wechat_clawbot', enabled=False))
    result = upgrade_operations._send_final_receipt(operation_id='op',
        receipt_target={'channel': 'wechat', 'sender_id': 'sender', 'bot_config_path': bot_path,
            'reply_context': {'to_user_id': 'sender', 'context_token': 'ctx'}},
        payload={}, preview={}, result={}, status='applied', enabled=True, reply_fn=other, wechat_reply_fn=send)
    assert result['error_code'] == 'CHANNEL_RECEIVE_DISABLED'
    assert send.call_count == 1
    other.assert_not_called()


def test_service_readiness_is_selected_and_read_only(controls, monkeypatch):
    from src.application.service_deploy import service_status_from_profile
    bot_path = controls('feishu', receive_enabled=False)
    forbidden = Mock(side_effect=AssertionError('service command called'))
    profile = {'bot_config_path': bot_path, 'services': [{'name': 'options-monitor-tick-us.service'},
        {'name': 'options-monitor-feishu-ws.service'}]}
    result = service_status_from_profile(profile, run_cmd=forbidden)
    assert 'channel_readiness' not in result['services'][0]
    assert result['services'][1]['channel_readiness']['error_code'] == 'CHANNEL_RECEIVE_DISABLED'
    forbidden.assert_not_called()


def test_keepalive_disable_between_targets_stops_without_consuming_state(controls, tmp_path):
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    path = controls('wechat_clawbot')
    store = WechatClawbotStateStore(tmp_path / 'wechat')
    store.save_bindings({'bindings': {
        'a': {'to_user_id': 'a', 'context_token': 'ca'},
        'b': {'to_user_id': 'b', 'context_token': 'cb'},
    }})
    def get_config(**kwargs):
        controls('wechat_clawbot', receive_enabled=False)
        return {'ret': 0}
    client = SimpleNamespace(get_config=Mock(side_effect=get_config))
    state = {}
    result = wechat._maybe_keepalive_bound_context(store=store, state=state, client=client,
        interval_sec=1, bot_config_path=path)
    assert result['error_code'] == 'CHANNEL_RECEIVE_DISABLED'
    assert client.get_config.call_count == 1
    assert state == {}
    result = wechat._maybe_keepalive_bound_context(store=store, state=state, client=client,
        interval_sec=1, bot_config_path=path)
    assert result['attempted'] is False
    assert client.get_config.call_count == 1


def test_ack_guard_rechecks_after_facade_preparation(controls):
    path = controls('feishu')
    settings = feishu_ws.FeishuWsSettings(bot_config_path=path, app_id='a', app_secret='{{SECRET_w7c60lmt}}', ack_reaction='Typing')
    sent = Mock()
    def reaction(**kwargs):
        controls('feishu', enabled=False)
        kwargs['before_send']()
        sent()
    inbound = {'ok': True, 'data': {'kind': 'message', 'request': {'message_id': 'm'},
        'inbound_result': {'ok': True}}}
    result = feishu_ws._maybe_react(inbound=inbound, settings=settings, reaction_fn=reaction)
    assert result['error_code'] == 'CHANNEL_RECEIVE_DISABLED'
    assert result['attempted'] is False
    sent.assert_not_called()


def test_feishu_retry_cannot_disable_then_enter_business_facade(controls, monkeypatch):
    path = controls('feishu')
    settings = feishu_ws.FeishuWsSettings(bot_config_path=path)
    def retry(**kwargs):
        controls('feishu', receive_enabled=False)
        return {'attempted': False}
    monkeypatch.setattr(feishu_ws, '_retry_pending_feishu_reply', retry)
    forbidden = Mock(side_effect=AssertionError('disabled event reached business'))
    result = feishu_ws.handle_feishu_ws_event({}, settings=settings,
        channel_service=SimpleNamespace(handle_inbound=forbidden), reply_fn=forbidden, reaction_fn=forbidden)
    assert result['error']['code'] == 'CHANNEL_RECEIVE_DISABLED'
    forbidden.assert_not_called()


def test_missing_plugin_only_breaks_selected_channel_operation(controls, tmp_path, monkeypatch):
    controls()
    monkeypatch.setattr(plugins, 'channel_plugin_installed', lambda channel: channel != 'wechat_clawbot')
    forbidden = Mock(side_effect=AssertionError('missing plugin called'))
    with pytest.raises(AgentToolError) as error:
        wechat.poll_wechat_clawbot_once(base=tmp_path, client_factory=forbidden)
    assert error.value.code == 'CHANNEL_PLUGIN_NOT_INSTALLED'
    policy.require_inbound_channel('feishu')
    forbidden.assert_not_called()


def test_live_disable_before_second_poll_never_calls_provider(controls, tmp_path):
    path = controls('wechat_clawbot')
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    store = WechatClawbotStateStore(tmp_path / 'state')
    store.save_state({'bot_token': 'fixture-token'})
    get_updates = Mock(return_value={'ret': 0, 'msgs': []})
    client_factory = Mock(return_value=SimpleNamespace(get_updates=get_updates))
    kwargs = dict(base=tmp_path, state_dir=str(store.state_dir), bot_config_path=path,
        reply_enabled=False, client_factory=client_factory)
    assert wechat.poll_wechat_clawbot_once(**kwargs)['ok']
    assert get_updates.call_count == 1
    controls('wechat_clawbot', receive_enabled=False)
    with pytest.raises(AgentToolError):
        wechat.poll_wechat_clawbot_once(**kwargs)
    assert get_updates.call_count == 1
    assert client_factory.call_count == 1


def test_queued_ack_is_blocked_by_config_change_before_worker(controls):
    import time
    path = controls('feishu')
    forbidden = Mock(side_effect=AssertionError('disabled ACK was sent'))
    worker = feishu_ws._FeishuAckWorker(settings=feishu_ws.FeishuWsSettings(bot_config_path=path),
        reaction_fn=forbidden)
    assert worker.submit(message_id='m', emoji_type='Typing', received_monotonic=time.monotonic(),
        event_ref='fixture') == 'accepted'
    controls('feishu', receive_enabled=False)
    worker._queue.put_nowait(None)
    worker._run()
    forbidden.assert_not_called()


def test_disabled_upgrade_receipt_does_not_claim_pending_outbox(controls):
    path = controls('feishu', enabled=False)
    store = SimpleNamespace(get_outbox=lambda _: {'status': 'pending', 'payload': {'enabled': True,
        'receipt_target': {'channel': 'feishu', 'bot_config_path': path}}}, claim_outbox=Mock())
    forbidden = Mock(side_effect=AssertionError('disabled final receipt sent'))
    result = upgrade_operations._dispatch_final_receipt_outbox(store=store, outbox_id='outbox',
        reply_fn=forbidden, wechat_reply_fn=forbidden)
    assert result['attempted'] is False
    assert result['error_code'] == 'CHANNEL_RECEIVE_DISABLED'
    store.claim_outbox.assert_not_called()
    forbidden.assert_not_called()


def test_missing_authoritative_config_blocks_analysis_control(controls, tmp_path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError('unauthorized cancellation'))
    monkeypatch.setattr('src.application.bot.channel_facade.cancel_channel_analysis', forbidden)
    with pytest.raises(AgentToolError) as error:
        feishu.prepare_feishu_analysis_control({}, allowed_senders='sender', config_key=None,
            config_path=None, audit_db=None, received_monotonic=0,
            bot_config_path=str(tmp_path / 'missing-bot.json'))
    assert error.value.code == 'CONFIG_ERROR'
    forbidden.assert_not_called()


def test_binding_poll_obeys_receive_disable_and_preserves_existing_binding(controls, tmp_path):
    from src.application.channels.wechat_clawbot.binding import bind_wechat_clawbot_target
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    path = controls('wechat_clawbot', receive_enabled=False, notify_enabled=True)
    store = WechatClawbotStateStore(tmp_path / 'state')
    store.save_state({'bot_token': 'fixture-token', 'get_updates_buf': 'original'})
    store.save_bindings({'bindings': {'prior': {'to_user_id': 'sender', 'context_token': 'still-valid'}}})
    before = store.bindings_path.read_bytes(), store.state_path.read_bytes()
    forbidden = Mock(side_effect=AssertionError('disabled binding poll'))
    with pytest.raises(AgentToolError):
        bind_wechat_clawbot_target(base=tmp_path, name='next', state_dir=str(store.state_dir),
            bot_config_path=path, client_factory=forbidden)
    assert (store.bindings_path.read_bytes(), store.state_path.read_bytes()) == before
    assert plugins.channel_activity_enabled(json.loads(open(path).read()), 'wechat_clawbot', 'notify')
    forbidden.assert_not_called()


def test_ack_real_facade_rechecks_after_token_resolution(controls, monkeypatch):
    from functools import partial
    from src.infrastructure import feishu_bot
    path = controls('feishu')
    def token_retry(app_id, app_secret, action, **kwargs):
        assert kwargs == {'token_timeout': 2, 'token_retry_max_attempts': 1, 'token_lock_timeout': 0.0}
        controls('feishu', receive_enabled=False)
        return action('synthetic-token')
    monkeypatch.setattr(feishu_bot, 'with_tenant_token_retry', token_retry)
    http = Mock(side_effect=AssertionError('disabled ACK HTTP request'))
    settings = feishu_ws.FeishuWsSettings(bot_config_path=path, app_id='app', app_secret='{{SECRET_o851exwb}}', ack_reaction='Typing')
    inbound = {'ok': True, 'data': {'kind': 'message', 'request': {'message_id': 'm'},
        'inbound_result': {'ok': True}}}
    result = feishu_ws._maybe_react(inbound=inbound, settings=settings,
        reaction_fn=partial(feishu_bot.add_message_reaction, http_json_fn=http))
    assert result['error_code'] == 'CHANNEL_RECEIVE_DISABLED'
    assert result['attempted'] is False
    http.assert_not_called()


def test_upgrade_wechat_real_reply_facade_rechecks_after_client_preparation(controls, tmp_path, monkeypatch):
    from src.application.channels.wechat_clawbot import reply
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    path = controls('wechat_clawbot')
    store = WechatClawbotStateStore(tmp_path / 'wechat')
    store.save_state({'bot_token': '{{SECRET_ba3qfi7i}}token'})
    send = Mock(side_effect=AssertionError('disabled final reply HTTP request'))
    def client_factory(**kwargs):
        controls('wechat_clawbot', enabled=False)
        return SimpleNamespace(send_text_message=send)
    with pytest.raises(AgentToolError) as blocked:
        reply.reply_wechat_clawbot_text(base=tmp_path, state_dir=str(store.state_dir), to_user_id='sender',
            context_token='ctx', text='final', client_factory=client_factory,
            before_send=lambda: policy.require_inbound_channel('wechat_clawbot', path))
    assert blocked.value.code == 'CHANNEL_RECEIVE_DISABLED'
    send.assert_not_called()
