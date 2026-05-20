# options-monitor 配置与表结构说明（实战版）

> 目标：你只要维护：
> - 可选 `configs/user.common.json`（US/HK 共用的用户覆盖）
> - `configs/user.us.json` / `configs/user.hk.json`（市场账号与 symbols；市场私有覆盖按需放这里）
> - 必要的环境变量（Feishu App 凭证与 Bitable 表引用）
> - `portfolio.data_config` 只作为可选兼容/迁移文件；`option_positions` 的稳态读写主存储由 `runtime_root` 固定派生到 SQLite

---

## 0) 最终保留哪几个配置文件？

### 推荐编辑入口（分层配置）
- `configs/system.json`：系统默认值，通常不需要用户改
- `configs/user.common.json`（可选）：US/HK 共用用户覆盖，同字段会被 market user 覆盖
- `configs/user.us.json`
- `configs/user.hk.json`

用户日常只维护 `configs/user.us.json` / `configs/user.hk.json` 里的 market-specific 账号和 symbols；如果某些覆盖 US/HK 都相同，放到可选的 `configs/user.common.json`。运行前生成 canonical runtime config：

```bash
cp configs/examples/user.common.example.json configs/user.common.json  # 可选
cp configs/examples/user.example.us.json configs/user.us.json
cp configs/examples/user.example.hk.json configs/user.hk.json
./om config build --market us
./om config build --market hk
```

`config build` 会在生成的 `config.us.json` / `config.hk.json` 写入 `_generated`
元信息，记录 system/common/user 三类源文件的路径和 SHA-256。之后只要
`configs/system.json`、`configs/user.common.json`、`configs/user.us.json` 或
`configs/user.hk.json` 有变化，就要重新 build 对应 market 的 runtime config。生产 tick
入口会检查这个指纹，避免 cron 拿陈旧 runtime config 继续跑。

不确定某个值来自哪里时，用 explain 查看覆盖链：

```bash
./om config explain --market us --key option_positions.auto_close.enabled
./om config explain --market us --key symbol_defaults.fetch.limit_expirations
```

生成产物仍是 runtime 唯一入口：
- `config.us.json`
- `config.hk.json`

### 兼容的运行时文件
- `config.us.json`
- `config.hk.json`
- `portfolio.runtime.json`（可选；只用于 external_holdings 的 Feishu 表 env 名声明或 legacy 迁移）

### 仓库里保留的模板文件
- `configs/system.json`
- `configs/examples/user.common.example.json`
- `configs/examples/user.example.us.json`
- `configs/examples/user.example.hk.json`
- `configs/examples/portfolio.runtime.example.json`
- `configs/examples/openclaw.profile.example.json`

### 最小配置和补充配置怎么区分？
- 最小编辑配置：`configs/user.us.json` / `configs/user.hk.json` 里的账号和 symbols；共用覆盖可放 `configs/user.common.json`
- 最小运行配置：生成后的 `config.us.json` / `config.hk.json`；期权持仓 SQLite 固定在 `<runtime_root>/output_shared/state/option_positions.sqlite3`
- 补充配置：在同一套结构上继续补 `watchdog.*`、`notifications.*`、`runtime.*`、`alert_policy.change_annual_threshold`、`intake.*`、`symbol_defaults.*`、`portfolio.source_by_account`、`feishu.*`
- 不再维护“两套 schema”或“两份不同风格文档”；只有一套结构，只是填写程度不同。

---

## 1) 本项目需要哪些外部“表”（Bitable）？

期权持仓不再需要单独数据配置文件。SQLite 主库固定由 `runtime_root` 派生。
如果启用 Feishu holdings 数据源，直接通过环境变量提供 Feishu App 凭证和 holdings 表引用。
- `holdings`：可选主数据源，提供现金与股票持仓（用于 base 现金、shares、avg_cost）
- `option_positions`：SQLite 主存储，提供已卖出期权占用（用于：
  - sell call 锁股数 `locked_shares_by_symbol`
  - cash-secured put 占用 `cash_secured_by_symbol`
)
- Feishu 不再承载 `option_positions`：不做 bootstrap，也不做镜像输出。

**你需要给我的信息（不含密钥）**：
- holdings 表的 Bitable 链接（或 app_token/table_id）
- holdings 表里字段名是否与下文一致（截图/字段列表即可）

---

## 2) holdings 表：字段要求（portfolio_context_builder）

应用模块：`src.application.portfolio_context_builder`

### 2.1 过滤逻辑
- 读取全表后按两列过滤：
  - `broker`：标准字段，要求该字段的字符串 **包含** config 里传入的 market/broker（容错匹配）
  - `market`：历史兼容字段；仅当 `broker` 缺失时回退使用，同样走“包含匹配”
  - `account`：若传入 account，则要求 **完全相等**

> 注意：holdings 的 market 是“包含匹配”，option_positions 是“完全相等”（见下文）。

### 2.2 必需字段（字段名必须一致）
通用：
- `asset_type`：字符串，至少需要支持：
  - `cash`
  - `us_stock`
- `broker`：标准字段，字符串（如：`富途`）
- `market`：历史兼容字段；仅当旧表还未补 `broker` 时继续兼容
- `account`：字符串（如：`lx`）

#### A) 现金行（asset_type = cash）
- `quantity`：现金数额（可为字符串，会被转 float）
- `currency`：币种（如 `USD` / `CNY`；脚本会 upper）

#### B) 股票行（asset_type = us_stock）
- `asset_id`：标的代码（如 `NVDA`），会转 upper
- `quantity`：持股数（会转 int）
- `avg_cost`：成本价（可空）
- `currency`：币种（可空）

### 2.3 输出给监控系统的关键字段
该脚本最终输出 JSON：
- `cash_by_currency`：例如 `{ "CNY": 516696.0, "USD": 1234.0 }`
- `stocks_by_symbol`：例如 `NVDA: {shares, avg_cost, ...}`

---

## 3) option_positions 表：字段要求（option_positions_context_builder）

应用模块：`src.application.option_positions_context_builder`

### 3.1 过滤逻辑（更严格）
- `market`：要求字段值 **完全等于** config 里传入的 market（如 `富途`）
- `account`：若传入 account，则要求 **完全相等**

### 3.2 必需字段（字段名必须一致）
通用：
- `market`
- `account`
- `symbol`：标的（如 `NVDA`），会转 upper

状态过滤：
- `status`：必须为 `open` 才计入占用
  - 也支持把 `status=open` 写在 `note` 字段里（key=value 形式）

合约类型/方向：
- `option_type`：`call` / `put`（也支持在 `note` 里写 `option_type=call`）
- `side`：`short` / `long`（也支持在 `note` 里写 `side=short`）

数量与占用：
- `contracts`：合约张数（float→int）
- `underlying_share_locked`（推荐字段名）：sell call 锁定股数
  - 兼容字段：`underlying_shares_locked`
  - 如果为空且是 short call，会按 `contracts * 100` 推算
- `cash_secured_amount`：short put 的现金担保占用（美元数值）

备注字段（可选）：
- `note`：可写 `key=value`；脚本支持 `status/option_type/side` 从 note 里解析

### 3.3 输出给监控系统的关键字段
- `locked_shares_by_symbol`：用于 sell call 可卖张数 = (shares - locked)/100
- `cash_secured_by_symbol`：用于卖 put 的“已占用担保现金”

---

## 4) config.us.json 或 config.hk.json：你需要配置什么？

安装版默认文件：`config.us.json` 或 `config.hk.json`

### 4.0A 配置优先级（只认这一套主路径）

对于操作者，运行时配置只需要理解这一套优先级：

1. 显式传入的 `config_path`
2. 显式传入的 `config_key`（`us` / `hk`）对应的 canonical config：`config.us.json` / `config.hk.json`
3. 未显式传入时，按入口默认值回落到 repo-local canonical config

`portfolio.data_config` 的解析规则也只认一套：

1. payload/命令里显式传入的 `data_config`
2. runtime config 里的 `portfolio.data_config`
3. 若都未提供，则按当前 runtime config 所在目录推导 `portfolio.runtime.json`
4. `OM_DATA_CONFIG` 只作为显式 override 使用，不属于主配置心智

不要把历史兼容文件名、旧 market-specific 变体、或额外 fallback 路径当作正式入口来理解。

### 4.0 先看最小配置：哪些字段一定要有？

#### runtime config 最小必需
- `accounts`
- `trade_intake.account_mapping.futu`
- `templates`
- `portfolio.broker`
- `portfolio.account`
- `portfolio.source`
- `portfolio.base_currency`
- `schedule`
- `symbols`

#### data_config
- 最小部署不需要 `portfolio.data_config`。
- 只有 external_holdings 需要声明 Feishu 表 env 名，或执行 legacy SQLite 迁移时，才使用 `portfolio.runtime.json`。

#### 最小配置对应的数据来源
- 行情与期权链：OpenD
- 持仓与现金：OpenD
- `option_positions`：SQLite

#### 最小配置下默认不需要
- `notifications.*`
- `runtime.*`
- `alert_policy.change_annual_threshold`
- `fetch_policy.*`
- `intake.*`
- `portfolio.source_by_account`
- `feishu.*`

#### 配置检查与运行检查的边界

只需要记住这一张表：

| 工具 | 负责什么 | 不负责什么 |
|---|---|---|
| `./om config validate --market us|hk` | 配置结构、字段语义、removed/legacy 字段、数值约束、市场 schedule 时区契约、runtime config 生成指纹 | OpenD 是否在线、环境变量是否已注入、runtime 输出是否健康 |
| `config_validate` | 基础 runtime config 结构校验 | OpenD 是否在线、环境变量是否已注入、生成指纹是否最新 |
| `healthcheck` | runtime config 可读、SQLite store、Feishu env readiness、OpenD readiness、option_positions bootstrap 状态 | 不负责替代主配置语义文档 |
| `runtime_status` | 只读汇总现有 runtime / OpenClaw 输出文件 | 不校验配置语义，不检查 OpenD |
| `openclaw_readiness` | 组合 `runtime_status` + `healthcheck` + 本地 openclaw 可用性 | 不替代 `config_validate` 的纯配置语义检查 |

判断规则很简单：
- 配置本身写得对不对，看 `config_validate`
- runtime config 是否由最新 system/common/user 生成，看 `./om config validate --market us|hk`
- 环境能不能跑起来，看 `healthcheck` / `openclaw_readiness`
- 历史运行结果长什么样，看 `runtime_status`

### 4.1 accounts：账户列表
- `accounts`: 统一 tick 运行和辅助脚本的默认账户列表，例如 `["lx", "sy"]`。
- 当前没有独立的“单账户链路”和“多账户链路”；`./om run tick --accounts lx` 是单账户运行，`./om run tick --accounts lx sy` 是多账户运行。
- 脚本命令行显式传 `--accounts` 时，以命令行为准。
- `notifications.cash_footer_accounts` 仅在你要指定“部分账户带现金 footer”时才配置；未配置时会回退到 `accounts`，避免与账户列表重复维护。

### 4.2 templates：通用底线（复用）
- `templates.put_base.sell_put.min_annualized_net_return`：全局 put 最低年化（例如 0.10）
- `templates.*.*.min_net_income`：全局最低单笔净收益，统一按 CNY 配置；运行时会按标的币种换算为 USD/HKD 后传给扫描器。
- `templates.call_base.sell_call.min_strike_cost_multiplier`：sell_call 的成本价 strike 下限倍数；模板默认 `1.02`，表示有效 `min_strike` 至少为 `avg_cost * 1.02`。
- `sell_put.min_annualized_net_return` 统一解析优先级：
  `symbol.sell_put.min_annualized_net_return` > `templates.<name>.sell_put.min_annualized_net_return` > 代码默认 `DEFAULT_MIN_ANNUALIZED_NET_RETURN(0.07)`。
- 全局流动性/价差硬过滤仅允许 3 个键：`min_open_interest`、`min_volume`、`max_spread_ratio`
- `templates.call_base.sell_call.*`：sell_call 的通用底线

### 4.3 symbols[]：每个标的的个性化区间
你通常只需要改：
- sell_put：`min_dte/max_dte`、`min_strike/max_strike`
  - put / call 现在统一按“边界模式”规划抓取窗口，只是方向相反。
  - put 的近端边界是 `max_strike`；若只配置了 `max_strike`，抓取层会自动向下扩 `20%` 作为抓取下界。
  - `min_strike=0` 已废弃；若不想设置下界，直接省略 `min_strike`。
- sell_call（enabled 时）：`min_strike`（以及 dte 范围）
  - `avg_cost/shares` 已移除：sell_call 仅从 holdings 自动读取。
  - `min_strike_cost_multiplier` 会用自动读取的 `avg_cost` 做硬过滤；例如 `1.02` 表示有效 `min_strike` 不低于 `avg_cost * 1.02`。
  - 若 holdings 取不到（该账户缺 holdings / 读取失败），则该账户的 sell_call 会被跳过。
  - 抓取层现在会先为 sell_put / sell_call 分别规划 required_data 窗口，再按相同 expiration 尽量合并到底层 OpenD 请求。
  - sell_call 的近端边界是 `min_strike`；若只配置了 `min_strike`，抓取层会自动向上扩 `20%` 作为抓取上界。
  - 若 sell_call 未配置任何 strike 边界，抓取层会退回到基于 `spot` 的默认窗口 `[spot*1.03, spot*1.20]`。
  - 旧的按 OTM% 定义 call 抓取窗口的配置已移除，避免与绝对价边界模式重复定义同一抓取窗口。
  - sell_call 抓取窗口允许小幅 buffer，仅用于避免边界漏抓；扫描阶段仍严格使用原始 `min_strike/max_strike`。
- `use`: 选择使用哪些模板（例如 `["put_base","call_base"]`）
- `fetch.source`: 行情源，当前 symbol required-data 运行时仅支持 `futu`（富途数据源，经本机 OpenD 网关 + Futu API）；旧值 `opend` 仍兼容。
- `yahoo` / `yfinance` 不作为 symbol required-data 的受支持运行时来源；它们只保留给独立的事件风险数据抓取等非 OpenD fallback 场景。

### 4.4 portfolio：账户约束来源
- `data_config`: 可选迁移配置；最小部署不需要，正式路径由 runtime root 与环境变量决定
- `broker`: 对外公开配置名，用来过滤 holdings / option_positions（例如 `富途`）
- `market`: 兼容旧配置的别名；新配置不再推荐继续使用
- `account`: 用来过滤两张表（例如 `lx`）
- `source`: `auto` / `futu` / `holdings`，作为全局默认 portfolio 来源；最小配置建议固定 `futu`
- `source_by_account`: 可选，按账户覆盖 `source`，例如 `{ "user1": "futu" }`
  - 解析优先级：`source_by_account[account] -> source -> auto`
- `base_currency`: 当前策略口径（CNY）
- `account_settings.<account>.type`:
  - `futu`: 主路径走 Futu/OpenD
  - `external_holdings`: 只有 Feishu holdings，没有 Futu `acc_id`
- `account_settings.<account>.holdings_account`:
  - 对 `futu` 账号：当该账号显式使用 `holdings` 数据源时，对应的 `holdings.account`
  - 对 `external_holdings` 账号：该账号在 Feishu holdings 里的实际名称
- `account_settings.<account>.bitable.*`:
  - 当前只作为历史/预留展示字段保留
  - 不参与 runtime holdings 连接配置
  - runtime 唯一生效的 Feishu holdings 来源是 env file 里的 `OM_FEISHU_HOLDINGS_TABLE`（或 `portfolio.runtime.json` 内声明的替代 env 名）
- `account_settings.<account>.futu.host` / `account_settings.<account>.futu.port`:
  - 可选，账户级 OpenD 持仓连接参数。
  - 当前 runtime 已支持按账户读取不同的 OpenD holdings 端点。
  - 解析优先级：
    1. `account_settings.<account>.futu.host/port`
    2. `portfolio.futu.host/port`
    3. `symbols[].fetch.host/port`
    4. 系统默认值
- `account_settings.<account>.futu.account_id`:
  - 可选，仅作为该账户对应 Futu 账户信息的一部分保留；实际持仓过滤仍依赖 `trade_intake.account_mapping.futu`。

#### 4.4.1 每账户不同 OpenD 持仓：推荐配置示例

```json
{
  "accounts": ["lx", "sy"],
  "account_settings": {
    "lx": {
      "type": "futu",
      "market": "us",
      "futu": {
        "host": "192.168.1.10",
        "port": 11111,
        "account_id": "12345678"
      }
    },
    "sy": {
      "type": "futu",
      "market": "us",
      "futu": {
        "host": "192.168.1.20",
        "port": 11111,
        "account_id": "87654321"
      }
    }
  },
  "trade_intake": {
    "receipt": {
      "enabled": true,
      "notify_applied": true,
      "notify_unresolved": true,
      "notify_failed": true,
      "notify_duplicate": false,
      "retry_unconfirmed_duplicate": true
    },
    "account_mapping": {
      "futu": {
        "12345678": "lx",
        "87654321": "sy"
      }
    }
  }
}
```

说明：
- 不同账户现在可以实际走不同 OpenD holdings 端点。
- 旧的全局 `portfolio.futu` 和 `symbols[].fetch.host/port` 仍可继续作为兼容默认来源。
- 这次升级完成的是 **持仓/现金 context 的 per-account OpenD runtime 支持**，不是所有市场数据缓存都已经做成多 gateway 完全隔离。
- `trade_intake.receipt.enabled` 默认 `true`。apply 模式下，成交写入/未解析/失败后会按 `notify_applied`、`notify_unresolved`、`notify_failed` 发送回执；重复 deal 默认不重复通知，但若上一次回执未确认，会按 `retry_unconfirmed_duplicate` 重试。
- `option_positions.auto_close.enabled` 控制专用过期自动平仓入口是否工作。
- `option_positions.auto_close.receipt.enabled` 默认 `true`。`./om option-positions auto-close-expired --apply` 实际写入或失败时，会按 `notify_applied` / `notify_failed` 发送回执；`notify_noop` 和 `notify_dry_run` 默认 `false`，避免无变更或 dry-run 产生噪音。回执会按账户、券商、业务日和平仓记录生成 `receipt_key`，同一业务日已确认发送的结果不会重复通知；`retry_unconfirmed` 默认 `true`，上一条回执未确认时允许后续定时/人工重跑重试。

#### 4.4.2 auto trade intake multiplier fallback

自动成交 intake 写入 open 事件前会先把 broker raw payload 里的 symbol canonicalize 到共享格式（例如 `POP` / `HK.09992` / `HK.POP260528P150000` -> `9992.HK`），再解析 multiplier。fallback 顺序固定为：

1. payload / lookup row 显式字段：`multiplier`、`contract_multiplier`、`lot_size`
2. contract metadata：本地 `output_shared/state/multiplier_cache.json`，缺失时可按 listener 的 OpenD `host/port` 和 `runtime.opend_rate_limits.option_chain` 限频刷新；旧字段 `runtime.option_chain_fetch` 仍兼容
3. `intake.multiplier_by_symbol[canonical_symbol]`
4. 显式配置的 market default：`intake.default_multiplier_hk` / `intake.default_multiplier_us`

当所有来源都失败时，open deal 会进入 `unresolved_deal_ids`，并带 `retryable=true`、`missing_fields`、`multiplier_resolution.attempted_sources` 等诊断，方便补 cache/config 后重试。market default 只在配置中存在时使用，不作为代码里的隐式假设。

对 onboarding / starter config，不建议预置 `default_multiplier_hk` / `default_multiplier_us`。更安全的顺序是：
- 先依赖 payload / lookup row 的显式 multiplier
- 再依赖本地 `multiplier_cache.json` 或 OpenD 刷新
- 若仍不够，再按具体标的显式配置 `intake.multiplier_by_symbol`

### 4.5 notifications：推送目标
- `provider`: 通用投递器，当前主流程使用 `openclaw`
- `channel`: OpenClaw 传输通道，当前微信 Clawbot 使用 `openclaw-weixin`
- `target`: OpenClaw 目标字符串
- `quiet_hours_beijing`: 可选，北京时间免打扰窗口；不需要时直接省略，不要写 `null`
- `send_timeout_sec`: 可选，OpenClaw 单次发送超时，默认 60 秒，上限 300 秒
- `cash_footer_accounts` / `cash_footer_timeout_sec` / `cash_snapshot_max_age_sec`: 可选，现金摘要账户与查询参数
- `include_cash_footer`: 兼容旧 `scripts/run_pipeline.py` 的字段；多账户主流程不把它作为开关，主示例不再配置
- 不再推荐配置 `enabled` / `mode`，当前主流程不读取它们作为行为开关

微信 Clawbot 示例：

```json
{
  "notifications": {
    "provider": "openclaw",
    "channel": "openclaw-weixin",
    "target": "clawbot_target"
  }
}
```

说明：旧配置里的 `channel: "wechat_clawbot"` 会继续兼容并转换为 OpenClaw 实际通道名 `openclaw-weixin`。

### 4.6 schedule：监控时间窗口
- `timezone`: 业务运行窗口所在市场时区，例如美股 `America/New_York`、港股 `Asia/Hong_Kong`。不要用北京时间伪装市场时间；夏令时 / 冬令时由时区自动换算。
- `cron_interval_min`: 外部 cron / tick 触发频率，线上当前按 10 分钟一轮配置；它只用于允许轻微延迟补跑，不代表通知频率。
- `run_window`: 扫描和通知的业务运行窗口，字段为 `start`、`end`、`breaks`。港股午休等中场休市写在 `breaks`，休市窗口内会跳过。
- `run_points`: 窗口内真正允许扫描并通知的目标点。当前默认语义是开盘后 10 分钟一次、之后整点一次、收盘前 10 分钟一次。
- `gates`: 对运行目标点的额外约束。美股使用北京时间次日 02:00 前 gate，避免 02:00 以后继续扫描通知。

美股示例：

```json
{
  "schedule": {
    "enabled": true,
    "timezone": "America/New_York",
    "cron_interval_min": 10,
    "run_window": {
      "start": "09:30",
      "end": "16:00",
      "breaks": []
    },
    "run_points": {
      "start_plus_min": 10,
      "hourly_minute": 0,
      "end_minus_min": 10
    },
    "gates": [
      {
        "type": "before",
        "timezone": "Asia/Shanghai",
        "time": "02:00",
        "day_offset_from_window_start": 1
      }
    ]
  }
}
```

港股示例：

```json
{
  "schedule_hk": {
    "enabled": true,
    "timezone": "Asia/Hong_Kong",
    "cron_interval_min": 10,
    "run_window": {
      "start": "09:30",
      "end": "16:00",
      "breaks": [
        {"start": "12:00", "end": "13:00"}
      ]
    },
    "run_points": {
      "start_plus_min": 10,
      "hourly_minute": 0,
      "end_minus_min": 10
    }
  }
}
```

### 4.7 runtime：超时（线上稳定）
- `symbol_timeout_sec`：单标的 fetch/scan 超时
- `portfolio_timeout_sec`：读取 holdings/positions 超时
- `prefetch.max_workers`：required_data 预取并发；OpenD 限流敏感场景建议 US/HK 统一设为 `1`
- required_data 预取固定采用“完成优先”：即使某个标的触发 OpenD 限频或失败，也继续排队尝试剩余标的
- required_data 预取固定按启用策略的 DTE/行权价边界收窄抓取范围，减少冷缓存请求和 snapshot 面积
- required_data 同一轮会自动合并相同标的/同一 OpenD endpoint 的重复抓取请求，并在 `required_data_prefetch_summary.json` 写入 run 级取数汇总；这不是配置项
- OpenD option expiration 会按标的和交易日做本地缓存，减少同一轮和同一天重复发现到期日的请求；这不是配置项
- `opend_rate_limits.option_chain`：OpenD `get_option_chain` 共享频控，官方限频为 `10/30s`；当前可按完成优先把 `max_wait_sec` 调大；旧字段 `option_chain_fetch` 仍兼容
- `get_market_snapshot` 和 `get_option_expiration_date` 也有共享频控保护，默认按 OpenD 官方 `60/30s` 规则由代码兜底；通常不需要写进配置，除非官方规则变化或本机环境需要单独覆盖

示例：

```json
{
  "runtime": {
    "prefetch": {
      "max_workers": 1
    },
    "opend_rate_limits": {
      "option_chain": {
        "max_calls": 9,
        "window_sec": 30,
        "max_wait_sec": 600
      }
    }
  }
}
```

### 4.8 alert_policy：提醒变化阈值
- `change_annual_threshold`：年化变化达到该阈值才写入 changes
- `sell_put`：Sell Put 候选评级阈值（可选；缺省即下表默认值）
  - `high_annual`：年化净收益≥该值且 `high_spread_max` 同时满足，归为「优先」（默认 0.20）
  - `high_spread_max`：买卖价差比≤该值，配合 `high_annual` 触发「优先」（默认 0.20）
  - `medium_annual`：年化净收益≥该值，归为「可考虑」（默认 0.12）
- `sell_call`：Sell Call 候选评级阈值（可选；缺省即下表默认值）
  - `high_annual`：年化权利金回报≥该值且 `high_total` 同时满足，归为「优先」（默认 0.10）
  - `high_total`：行权情形下总收益≥该值，配合 `high_annual` 触发「优先」（默认 0.15）
  - `medium_annual`：年化权利金回报≥该值，归为「可考虑」（默认 0.06）

不写 `sell_put` / `sell_call` 时使用上述默认值，与历史硬编码行为一致。完整示例：

```json
{
  "alert_policy": {
    "change_annual_threshold": 0.02,
    "sell_put": {
      "high_annual": 0.20,
      "high_spread_max": 0.20,
      "medium_annual": 0.12
    },
    "sell_call": {
      "high_annual": 0.10,
      "high_total": 0.15,
      "medium_annual": 0.06
    }
  }
}
```

### 4.9 close_advice：平仓建议
- `enabled`: 是否生成平仓建议；关闭时仍会产出空文件，不会报错
- `quote_source`: `auto` / `required_data`
  - `auto`: 优先用 `required_data`，缺价格时再尝试通过 OpenD/Futu 补 quote
  - `required_data`: 只用本地 `required_data`，不额外发起 OpenD quote 补拉
- `notify_levels`: 哪些等级写入账户消息，默认建议 `["strong", "medium"]`
- `max_items_per_account`: 每个账户最多写入多少条平仓建议
- `max_spread_ratio`: 报价过宽时拒绝进入提醒的上限
- `strong_remaining_annualized_max`: `strong` 档剩余年化收益率上限
- `medium_remaining_annualized_max`: `medium` 档剩余年化收益率上限

建议起步配置：

```json
{
  "close_advice": {
    "enabled": true,
    "quote_source": "auto",
    "notify_levels": ["strong", "medium"],
    "max_items_per_account": 5,
    "max_spread_ratio": 0.3,
    "strong_remaining_annualized_max": 0.045,
    "medium_remaining_annualized_max": 0.07
  }
}
```

默认输出文件：
- 独立 close-advice 命令：默认写到 `output/reports/close_advice.csv` / `output/reports/close_advice.txt`
- 统一 tick 运行：按账户写到 `output_runs/<run_id>/accounts/<account>/close_advice.csv|txt`

### 4.10 手续费：内置规则
- `fees` 已不再支持配置。
- 当前默认内置规则：
  - US：富途美股期权完整手续费口径
  - HK：富途港股期权完整手续费口径
- 如果配置文件里仍带 `fees`，`validate_config` 会直接报错。

---

## 5) env file / Feishu App 凭证到底放哪？

### 最小方式（新部署）
- 不需要创建 repo-local `secrets` JSON。
- 不需要在 runtime config 里配置 `portfolio.data_config`。
- 期权持仓 SQLite 固定写入 `<runtime_root>/output_shared/state/option_positions.sqlite3`。
- 真实凭证放本机 env file：本地默认 `.env/options-monitor.env`，Linux 推荐 `/etc/options-monitor/options-monitor.env`。

配置后用只读命令确认来源和值已脱敏：

```bash
./om settings doctor
./om settings inspect
```

如果需要 legacy SQLite 迁移或 external_holdings 替代 env 名，才额外创建 `portfolio.runtime.json`。示例：

```json
{
  "option_positions": {
    "bootstrap_from_legacy_sqlite": {
      "enabled": false
    }
  },
  "feishu": {
    "app_id_env": "OM_FEISHU_APP_ID",
    "app_secret_env": "OM_FEISHU_APP_SECRET",
    "tables": {
      "holdings_env": "OM_FEISHU_HOLDINGS_TABLE"
    }
  }
}
```

- `option_positions.auto_close.receipt.enabled` 默认是 `true`，只影响专用过期自动平仓入口写入后的本地通知回执，不写 Feishu 镜像。每日维护 cron 或人工重跑触发同一批平仓时，代码会通过 `receipt_key` 做日级幂等；已确认回执不重复发，未确认回执可按 `retry_unconfirmed` 重试。
- 期权持仓的唯一主存储是本地 SQLite：`trade_events -> position_lots`。系统不再把期权持仓同步到 Feishu 多维表，也不再需要 `feishu.tables.option_positions`。
- Feishu 仍可用于 `external_holdings` 账号读取普通持仓；这是 holdings 数据源，不是期权持仓 ledger 镜像。

### 可选方式（增加 external_holdings 账号）
- 先执行：

```bash
./om-agent add-account --market us --account-label ext1 --account-type external_holdings --holdings-account "Feishu EXT"
```

- 设置环境变量：
  - `OM_FEISHU_APP_ID`
  - `OM_FEISHU_APP_SECRET`
  - `OM_FEISHU_HOLDINGS_TABLE=app_token/table_id`
- 如果需要替代 env 名，才在 `portfolio.runtime.json` 内配置 `feishu.app_id_env` / `feishu.app_secret_env` / `feishu.tables.holdings_env`。

示例：

```json
{
  "option_positions": {},
  "feishu": {
    "app_id_env": "OM_FEISHU_APP_ID",
    "app_secret_env": "OM_FEISHU_APP_SECRET",
    "tables": {
      "holdings_env": "OM_FEISHU_HOLDINGS_TABLE"
    }
  }
}
```

### 外部数据配置（旧部署迁移）
- 如果你已经在仓外维护数据配置 JSON，可以短期继续把 `portfolio.data_config` 指向该文件。
- 或设置环境变量 `OM_DATA_CONFIG=/absolute/path/to/portfolio.runtime.json`。

示例：

```json
{
  "option_positions": {},
  "feishu": {
    "app_id_env": "OM_FEISHU_APP_ID",
    "app_secret_env": "OM_FEISHU_APP_SECRET",
    "tables": {
      "holdings_env": "OM_FEISHU_HOLDINGS_TABLE"
    }
  }
}
```

当前仓库不再要求 repo-local `secrets/` 作为正式运行依赖；真实密钥通过环境变量注入。

> 注意：不要在聊天里发送 app_secret。
>
> `option_positions` bootstrap 的当前状态会出现在
> `./om-agent run --tool healthcheck ...` 的 `option_positions_bootstrap`。
> 如果配置了 Feishu bootstrap，但首次读取失败，这里会显示 degraded/warn，而不是把它伪装成“天然空库”。

---

## 6) 你怎么把“表和配置项”给我（不泄露密钥）

你可以发这些（任意一种即可）：
1) holdings 表的 Bitable 链接 + option_positions 表的 Bitable 链接
2) 或者直接发 `app_token/table_id`（例如 `xxx/tblxxx`），以及表的字段列表截图
3) 你当前 `config.us.json` 或 `config.hk.json`（可以直接发文件内容；里面不包含 secret）

**不要发**：Feishu app_secret、user_token。

---

## 7) 实战期：最短排障三件套

```bash
openclaw cron runs
cat /home/node/.openclaw/workspace/options-monitor-prod/output/state/last_run.json
cat /home/node/.openclaw/workspace/options-monitor-prod/<report_dir>/symbols_notification.txt  # 默认 report_dir=output/reports
```
