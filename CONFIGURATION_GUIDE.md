# options-monitor 配置指南

本文面向操作者，说明如何安全维护 `config.yaml`、生成运行快照并检查环境。配置事实链和禁止项见 [CONFIGS.md](CONFIGS.md)。

## 需要维护什么

普通安装只需要三类配置：

| 文件 / 来源 | 内容 |
|---|---|
| `config.yaml` | 账户、市场、symbols、策略与非 secret 行为 override |
| Keychain / systemd credentials | secrets、provider credential；逻辑名和迁移见 `docs/SECRET_STORAGE.md` |
| env-file | 非秘密本机设置和写入开关；`OM_SECRET_BACKEND=env` 仅为显式兼容 |
| 生成快照 | `config.us.json`、`config.hk.json`、`resolved/config.assistant.json` |

期权仓位不需要 Bitable：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

只有 `external_holdings` 账户需要 Feishu holdings 数据源；这不会让 Feishu 成为期权账本。

## 初始化

在源码 checkout 中：

```bash
./om config init \
  --output config.yaml \
  --runtime-output-dir .
```

按自己的券商账户和观察名单初始化时，直接在 starter 阶段传入本地账户形态，避免先生成 `lx` / `sy` 再手工大改：

```bash
./om config init \
  --output config.yaml \
  --runtime-output-dir . \
  --account-label christina \
  --futu-acc-id <futu-account-id> \
  --no-external-holdings \
  --us-symbol NVDA \
  --us-symbol AAPL \
  --hk-symbol 0700.HK
```

如果还有一套外部持仓来源，去掉 `--no-external-holdings`，或用 `--external-holdings-account <label>` 指定标签。

安装后的全局命令可去掉 `./`。

`config init` 默认生成：

- `config.yaml`
- `config.us.json`
- `config.hk.json`
- `config.assistant.json`

已有目标文件时默认拒绝覆盖；先检查差异，不要直接使用 `--force` 覆盖生产文件。

当前 starter 见 [config.yaml.example](configs/examples/config.yaml.example)。

## 最小 YAML

```yaml
accounts:
  lx:
    type: futu
    futu_account_id: "REPLACE_WITH_FUTU_ACCOUNT_ID"
  sy:
    type: external_holdings
    holdings_account: sy

markets:
  us:
    accounts: [lx, sy]
    symbols:
      - NVDA
      - GOOGL
    overrides:
      NVDA:
        sell_put:
          dte: [20, 45]
          strike: [80, 120]

  hk:
    accounts: [lx]
    symbols:
      - "0700.HK"
      - "9992.HK"
```

约定：

- 账户标签小写；
- 港股使用规范 `.HK` 代码并建议加引号；
- `markets.<market>.accounts` 只引用顶层已定义账户；
- `symbols` 保持字符串列表；
- 个性化策略配置放在 `overrides.<symbol>`；
- YAML 使用空格缩进，tab 会被拒绝。

系统默认值在 `src/application/config_defaults.py::DEFAULT_CONFIG`。不需要把所有默认字段复制进 `config.yaml`。

## 账户

支持两种账户类型：

### `futu`

```yaml
accounts:
  lx:
    type: futu
    futu_account_id: "REPLACE_WITH_FUTU_ACCOUNT_ID"
```

`futu` 账户的现金、股票持仓和可用 trade-intake 能力从账户设置派生。多 OpenD endpoint、host、port 和服务配置应通过当前示例、`config explain` 和 service preflight 核对，不要从历史 redesign plan 复制。

### `external_holdings`

```yaml
accounts:
  ext1:
    type: external_holdings
    holdings_account: "Feishu EXT"
```

`external_holdings` 从 holdings 数据源读取现金和普通持仓，交易默认人工录入。它不应启动 Futu trade-intake，也不应把 Feishu option position 当 canonical lot。

账户增删改应直接修改 `config.yaml`，然后 validate 并重建受影响的
runtime snapshot：

```bash
./om config validate --source yaml --market us --config-yaml config.yaml
./om config build --source yaml --market us \
  --config-yaml config.yaml \
  --output config.us.json
```

`./om-agent add-account` / `edit-account` / `remove-account` 是受控的
runtime-JSON 兼容 facade，不是 YAML authoring 入口；其结果会被下一次
`config build` 覆盖。只有明确需要该兼容路径时才先 `--dry-run`，再通过
`OM_AGENT_ENABLE_WRITE_TOOLS=true` 与 `--confirm` 写入精确目标。

## 市场与 symbol override

`markets.us` / `markets.hk` 分别定义：

- 本市场启用的 accounts；
- 本市场扫描的 symbols；
- 每个 symbol 的策略 override。

示例：

```yaml
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
    overrides:
      NVDA:
        sell_put:
          enabled: true
          dte: [20, 45]
          strike: [80, 120]
        covered_call:
          enabled: true
          dte: [20, 60]
          strike: [125, 160]
        combo_yield: true
```

YAML authoring 使用 `covered_call`；生成的 runtime、CSV 或 trace 可能使用内部 key `sell_call`。`combo_yield` 是当前开仓策略名，旧 `yield_enhancement` 只属于明确的兼容读取。

不要在静态文档里猜某个字段是否仍有效。检查来源和值：

```bash
./om config explain --source yaml \
  --market us \
  --key markets.us.overrides.NVDA.sell_put
```

## Assistant 与入站

`assistant` / `inbound` 仍写在 `config.yaml`，但运行时由独立快照消费：

```bash
./om config build-assistant --source yaml \
  --config-yaml config.yaml \
  --output resolved/config.assistant.json
```

模型 API key 只 provision 到固定逻辑凭据；YAML 选择 provider/model 即可。旧 `api_key_env`
仅在显式 `OM_SECRET_BACKEND=env` 的迁移模式下作为兼容名称使用。

Feishu long-connection、WeChat ClawBot 和本地 Assistant 共享 Control/Copilot 安全边界，但渠道 credential、sender allowlist 和 provider readiness 分别验证。详见：

- [Inbound Control](docs/INBOUND_CONTROL.md)
- [OM Copilot v2](docs/OM_COPILOT_V2_DESIGN.md)
- [Linux / Mac Deployment](docs/DEPLOY_LINUX_MAC.md)

## 通知

当前普通通知支持的 provider / channel 以配置验证器为准，主要包括：

- `wechat_clawbot`
- `feishu_app`

两者配置方式不同：

- WeChat 使用已有 binding / target；
- Feishu App recipient 来自受控环境变量，不应复用 WeChat `notifications.target`；
- webhook、App、入站 long-connection 和 external holdings 是不同角色。

不要复制旧 OpenClaw、Feishu option-position mirror 或 `notifications.daily_brief.enabled` 作为路由开关。scheduled ordinary notification 的 renderer authority 是 Daily Brief；兼容 preview 不具有 scheduled sender authority。

发送前先检查：

```bash
./om settings doctor
./om channel status \
  --runtime-root /var/lib/options-monitor \
  --profile-path /var/lib/options-monitor/service.profile.json \
  --env-file /etc/options-monitor/options-monitor.env
```

真实发送需要明确授权；不要用真实通知命令当连通性探针。

## env-file

Linux 推荐：

```text
/etc/options-monitor/options-monitor.env
```

macOS 推荐：

```text
$HOME/Library/Application Support/options-monitor/options-monitor.env
```

常见内容包括：

- Feishu App credential 与 recipient env；
- external holdings table env；
- LLM provider API key；
- Tool Gateway 写工具开关；
- runtime/service 机器级设置。

只读检查：

```bash
./om settings inspect
./om settings doctor
```

`settings inspect` 应只输出脱敏来源；若发现 secret 明文进入日志或配置输出，应停止后续操作。

## 生成运行快照

```bash
./om config validate --source yaml \
  --market us \
  --config-yaml config.yaml

./om config build --source yaml \
  --market us \
  --config-yaml config.yaml \
  --output config.us.json

./om config validate \
  --config-path config.us.json \
  --market us
```

HK 同理。生产建议把 YAML 和生成快照放在 release 外：

```text
/var/lib/options-monitor/config.yaml
/var/lib/options-monitor/config.us.json
/var/lib/options-monitor/config.hk.json
/var/lib/options-monitor/resolved/config.assistant.json
```

service profile 应记录这些显式路径。升级时缺少 YAML authoring source 会 fail closed；legacy JSON 不是升级恢复通道。

## 各检查入口的职责

| 入口 | 检查什么 |
|---|---|
| `om config validate --source yaml` | YAML 与 defaults 合并后的结构、removed 字段和语义 |
| `om config validate --config-path` | 生成 runtime JSON、市场契约和生成指纹 |
| `config_validate` Tool | runtime JSON 的基础结构 |
| `healthcheck` Tool | OpenD、SQLite、credential 与运行前置条件 |
| `runtime_status` Tool | 现有 run/service/state artifact，不替代 config validator |
| `om settings doctor` | env-file 和 provider setting readiness |
| `om service preflight` | 部署前 profile、路径和服务前置条件 |

推荐顺序：

```bash
./om config validate --source yaml --market us
./om config validate --config-path config.us.json --market us
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

## external holdings

需要 Feishu holdings 时，通过 env-file 提供 App credential 与 holdings table 引用。`portfolio.runtime.json` 只在必须替换默认 env 名等兼容场景使用。

它不能配置：

- Feishu `option_positions` bootstrap；
- Feishu `option_positions` mirror；
- 第二套期权持仓事实源。

需要向协作者提供诊断信息时，可以分享：

- 脱敏后的 `config.yaml`；
- `config explain` 输出；
- `settings doctor` 脱敏输出；
- holdings 字段名；
- `app_token/table_id` 的非 secret 部分。

不要分享 app secret、user token、webhook secret 或 LLM API key。

## 旧 layered JSON 迁移

旧 `configs/user.*.json` 只通过以下入口迁移：

```bash
./om config migrate-yaml --output config.yaml
```

默认 dry-run。核对 accounts、markets、symbols 和等价性后：

```bash
./om config migrate-yaml --output config.yaml --apply
```

CLI 不支持用 `--source legacy` 继续 build / explain。迁移后重新生成所有 runtime snapshot。

## 变更检查清单

每次配置变更至少确认：

1. 修改的是正确的 `config.yaml`；
2. 没有把 secret 或 write gate 写入 YAML；
3. US/HK 目标市场正确；
4. account 与 symbol 没有跨市场串用；
5. YAML validate 通过；
6. 对应 runtime JSON 已 rebuild；
7. runtime fingerprint 新鲜；
8. assistant 配置变化时已 rebuild assistant JSON；
9. `healthcheck` 没有新增阻断项；
10. 首次真实运行先 `--no-send`，并理解它仍会写本地 artifact。
