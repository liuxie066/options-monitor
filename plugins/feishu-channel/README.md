# Optional Feishu message transport

For ordinary installation use `om plugins install feishu` to preview and
`om plugins install feishu --confirm` to install the exact matching official
Release package. Installation does not enable the channel. See
[official installation](../../docs/INSTALL.md#官方可选插件); source capability does
not imply that a matching Release asset has been published.

For source development, install into the Python environment running OM:

```sh
python -m pip install ./plugins/feishu-channel
python -m pip uninstall om-feishu-channel-plugin
```

The distribution `om-feishu-channel-plugin` supplies `om_feishu_channel` and
depends on `lark-oapi`. Installation does not enable a channel, connect a client,
or send a message. This is an OM extension, not a standalone client SDK; it uses
the host application's contracts and shared Feishu HTTP/token/error owner.

REST send/reply/reaction implementations live in `om_feishu_channel.bot` and
the SDK connector in `om_feishu_channel.ws_client`. Existing imports through
`src.infrastructure.feishu_bot` and `src.infrastructure.feishu_ws_client` stay
available as explicit lazy facades. Injected HTTP functions and the facade's
current token-retry function are forwarded to the REST implementation.

Core retains pure renderers, payload builders, budgets and frozen-history
validation, along with authorization, inbound orchestration and durable reply
state. Bitable/portfolio data access stays independent of this message package.
Core imports and local rendering/history do not require the package; requesting
its transport without installation fails explicitly. No source-path fallback
loads this directory automatically.

For development, install this package and the WeChat channel package explicitly
before running the existing full test suite; do not skip channel tests. A
temporary installation can be used without modifying the shared environment:

```sh
python -m pip install --target /tmp/om-channel-test-packages ./plugins/feishu-channel ./plugins/wechat-channel
PYTHONPATH=/tmp/om-channel-test-packages python -m pytest tests/test_message_channel_optional_packages.py tests/test_message_channel_transports.py tests/test_feishu_bot.py tests/test_inbound_feishu_ws.py
```

Tests use injected or fake transports; installation and test success do not
establish live delivery or service readiness.
