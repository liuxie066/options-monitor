# Optional WeChat message transport

For ordinary installation use `om plugins install wechat` to preview and
`om plugins install wechat --confirm` to install the exact matching official
Release package. Installation does not enable the channel. See
[official installation](../../docs/INSTALL.md#官方可选插件); source capability does
not imply that a matching Release asset has been published.

For source development, install into the Python environment running OM:

```sh
python -m pip install ./plugins/wechat-channel
python -m pip uninstall om-wechat-channel-plugin
```

The distribution `om-wechat-channel-plugin` supplies `om_wechat_channel`, with
no third-party dependencies. Installation does not enable polling, log in,
bind a recipient, or send a message. This is an OM extension requiring the host
application's contracts, not a standalone SDK.

The iLink HTTP and protocol client live in `om_wechat_channel.ilink_client`.
The existing `src.application.channels.wechat_clawbot.ilink_client` import
retains constants, the shared `WechatClawbotError`, an explicit lazy HTTP wrapper
and a lazy client constructor. The returned client preserves the existing
`http_json_fn` injection. Package imports perform no network I/O.

OM core retains bindings, private state files, cursor handling, sender/reply
authorization, outbox claims and receipts. Removing this distribution does not
delete those records. Importing CLI/settings/report/history modules does not
load this package; requesting its transport requires an actual installation.
There is no source-tree fallback or module alias.

Development and CI must explicitly install both channel packages for the full
existing test suite. A `pip --target` installation on the test process's
`PYTHONPATH` is sufficient; see the Feishu package README for the command.
The optional-package tests independently block imports in child processes and
the protocol tests inject HTTP calls; neither test path needs live credentials.
