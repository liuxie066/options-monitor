# Futu 多账户 OpenD 改造方案

## 背景

`sy` 账户当前更像外部 holdings 账户：现金和股票持仓主要来自 holdings 表，交易也无法通过独立 Futu 登录自动接入。实际需求是 `sy` 必须以另一个独立富途账户登录，因此它应该成为完整的 `type=futu` 账户，而不是在交易、现金、持仓三个方向分别打补丁。

本方案不新增 `opend_session`、`gateway`、`data source lane` 等产品概念。OpenD 只是 `type=futu` 账户的连接参数。

## 目标模型

账户类型仍只有两类：

- `type=futu`：账户有 Futu API 私有数据源。
- `type=external_holdings`：账户没有可用 Futu API，依赖 holdings 表和手动交易录入。

推荐配置形态：

```yaml
account_settings:
  lx:
    type: futu
    futu:
      account_id: "281756479859383816"
      host: 127.0.0.1
      port: 11111
      telnet_port: 22222
      opend_root: /home/liuxie/apps/futu-opend-lx/current

  sy:
    type: futu
    trade_intake_enabled: true
    futu:
      account_id: "<SY_FUTU_ACCOUNT_ID>"
      host: 127.0.0.1
      port: 11112
      telnet_port: 22223
      opend_root: /home/liuxie/apps/futu-opend-sy/current
```

`external_holdings` 保持现状：

```yaml
account_settings:
  manual_account:
    type: external_holdings
    holdings_account: manual_account
```

## 推导规则

`type=futu`：

- 现金来源：Futu。
- 股票持仓来源：Futu。
- 交易来源：OpenD API push + history backfill。
- 必须配置 `futu.account_id`。
- 多 Futu 账户时必须显式配置 `futu.host` / `futu.port`，避免错误复用默认 OpenD。

`type=external_holdings`：

- 现金来源：holdings 表。
- 股票持仓来源：holdings 表。
- 交易来源：手动录入。
- 不启动 API trade-intake。

例外控制：

- `account_settings.<account>.trade_intake_enabled=false` 仅用于临时关闭某个 Futu 账户的 API 交易监听。
- 关闭交易监听不影响 Futu 现金和股票持仓读取。

## 模块设计

### 1. 账户配置模块

涉及文件：

- `src/application/account_config.py`
- `src/application/layered_config.py`
- `src/application/config_yaml.py`
- `src/application/config_validator.py`

改动：

- 扩展账户 view，使 `account_settings.<account>.type` 成为权威入口。
- 从 `account_settings.<account>.futu` 解析 `account_id`、`host`、`port`、`telnet_port`、`opend_root`。
- 派生只读能力：
  - `portfolio_source=futu|holdings`
  - `trade_source=api|manual`
  - `trade_intake_enabled`
- 继续兼容并派生旧字段：
  - `portfolio.source_by_account`
  - `trade_intake.account_mapping.futu`

不做：

- 不新增 `opend_session` 或 `futu_gateway` 概念。
- 不拆分 `cash_source` 和 `positions_source` 配置。

### 2. Futu 现金和股票持仓模块

涉及文件：

- `src/application/futu_portfolio_context.py`
- `src/application/portfolio_context_service.py`
- `src/application/cash_headroom_query.py`
- `src/application/pipeline_context.py`
- `src/application/multi_tick/cash_footer.py`

改动：

- `fetch_futu_portfolio_context(account=...)` 直接从账户配置读取：
  - `futu.account_id`
  - `futu.host`
  - `futu.port`
- 不再依赖 `trade_intake.account_mapping.futu` 才能读取现金和股票持仓。
- 保留现有 portfolio context 输出结构：
  - `cash_by_currency`
  - `cash_power_by_currency`
  - `cash_components_by_currency`
  - `stocks_by_symbol`
  - `portfolio_source_name`

不做：

- 不让 Futu 当前期权持仓覆盖本地 option-position ledger。
- 不把现金和股票持仓拆成两个可独立配置的数据源。

### 3. 交易接入模块

涉及文件：

- `src/application/trades/account_mapping.py`
- `src/application/trades/auto_intake.py`
- `src/application/trades/backfill.py`
- `src/application/trades/intake.py`
- `src/application/trades/normalizer.py`

改动：

- 根据账户配置找出所有可 API 接入的账户：
  - `type=futu`
  - `futu.account_id` 存在
  - `futu.host` / `futu.port` 存在
  - `trade_intake_enabled != false`
- 一个 `trade-intake` 进程内按 `host:port` 启动多个 listener/backfill。
- 每个账户或每个 OpenD endpoint 使用独立 state/audit/status 文件。
- 所有 listener 共享同一个 ledger repo，写入时使用进程内锁。

推荐状态路径：

```text
output_shared/state/trade_intake/lx/state.json
output_shared/state/trade_intake/lx/audit.jsonl
output_shared/state/trade_intake/lx/status.json

output_shared/state/trade_intake/sy/state.json
output_shared/state/trade_intake/sy/audit.jsonl
output_shared/state/trade_intake/sy/status.json
```

幂等增强：

```text
external_event_key = futu:<account>:<futu_account_id>:<deal_id>
```

该 key 用于避免不同 Futu 登录下 deal id 理论冲突。

### 4. 期权仓位账本模块

涉及文件：

- `src/application/trades/resolver.py`
- `src/application/ledger/api.py`
- `domain/domain/ledger/projection.py`
- reconcile 相关测试

保持不变：

- `trade_events -> projection -> position_lots` 仍是期权仓位唯一权威。
- 手动录入和 API intake 最终都归一到 trade events。
- assignment、expiry、assigned stock sale 的业务语义不重做。

增强：

- API 交易写入时保留：
  - `source=api`
  - `account`
  - `futu_account_id`
  - `source_deal_id`
  - `external_event_key`
- reconcile 查询 pending/failed deal 时带 account/source 维度。

### 5. OpenD 服务模块

涉及文件：

- `src/application/service_deploy.py`
- service profile
- service drift / runtime status 读取

改动：

- service render 根据 `account_settings.*.futu.opend_root` 渲染多个 OpenD service：
  - `options-monitor-opend-lx.service`
  - `options-monitor-opend-sy.service`
- `options-monitor-trade-intake.service` 依赖所有 Futu OpenD service。
- 如果只有一个 Futu 账户，保持当前单 OpenD 行为兼容。

不做：

- 不把 OpenD service 暴露成用户需要理解的新业务模块。

### 6. 健康度和诊断模块

涉及文件：

- `src/application/agent_tools/healthcheck_impl.py`
- `src/application/agent_tools/runtime_status_impl.py`
- `src/application/runtime_status_cli.py`

改动：

- 展示每个账户的派生状态：

```text
lx:
  type=futu
  portfolio=futu
  trade=api
  opend=127.0.0.1:11111
  trade_intake=listening

sy:
  type=futu
  portfolio=futu
  trade=api
  opend=127.0.0.1:11112
  trade_intake=listening
```

检查项：

- `type=futu` 必须有 `futu.account_id`。
- 多 Futu 账户必须有明确 `futu.host` / `futu.port`。
- `trade_intake_enabled=true` 时 OpenD 必须 ready。
- `type=external_holdings` 必须有 holdings 表配置。

## 实施顺序

1. 实现账户 runtime plan resolver 和配置验证。
2. 改 Futu 现金/股票持仓读取，使 `sy` 可从自己的 OpenD 读取账户快照。
3. 改 trade-intake 多账户监听，先 dry-run。
4. 改 service render，支持多 OpenD service。
5. 改 healthcheck/runtime_status。
6. 迁移生产 `sy`：
   - 先验证 Futu 现金和股票持仓。
   - 再启动第二个 OpenD。
   - 再开启 trade-intake dry-run/backfill。
   - 最后切到 apply。

## 验收标准

- `query_cash_headroom sy` 返回 Futu 现金。
- 扫描中 `sy` 的股票持仓来自 Futu。
- `trade-intake --once` 能展示 `lx` 和 `sy` 两个 API 账户。
- `runtime_status` 能展示两个 OpenD 状态和两个 trade-intake 状态。
- `sy` 的 push/backfill 交易写入 `sy` 账户的 trade events。
- `external_holdings` 账户仍走 holdings 表和手动交易录入。
- 现有 option-position ledger 权威链不变。

## 风险和约束

- 不能把 Futu 当前期权持仓直接当作本地期权仓位权威。
- 不能让两个独立 trade-intake 进程无隔离地写同一个 state 文件。
- 迁移生产前必须先 dry-run 和只读验证。
- 当前配置写入、服务安装、生产 state 修改都需要显式人工批准。
