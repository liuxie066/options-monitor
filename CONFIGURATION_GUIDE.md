# options-monitor 配置与表结构说明（实战版）

> 目标：你只要维护：
> - `config.yaml`：用户 override，放 accounts、markets、symbols 和非 secret 行为配置
> - env-file：Feishu App 凭证、Bitable 表引用、写入开关
> - 生成后的 runtime config：`config.us.json` / `config.hk.json`，供实际运行读取
> - `portfolio.data_config` 只作为可选兼容/迁移文件；`option_positions` 的稳态读写主存储由 `runtime_root` 固定派生到 SQLite

---

## 0) 最终保留哪几个配置文件？

### 推荐编辑入口

```text
src/application/config_defaults.py DEFAULT_CONFIG
  + config.yaml user overrides
  + env-file secrets/write gates
  -> config build
  -> runtime config JSON
```

- 系统默认值在代码里的 `DEFAULT_CONFIG`，用户不编辑默认配置文件。
- `config.yaml` 是推荐的人类编辑入口，只保存 override。
- `config.us.json` / `config.hk.json` 是生成后的 runtime config，不是首选手工编辑入口。
- env-file 保存 secrets、Feishu Bot 凭证和写入开关；`config.yaml` 会拒绝 write gate 字段。
- `config build` / `config explain` 默认读取 YAML；legacy JSON 只在显式 `--source legacy` 时使用。

本地/source checkout 可以直接在 repo root 维护 `config.yaml`：

```bash
./om config init --output config.yaml --runtime-output-dir .
$EDITOR config.yaml

./om config validate --source yaml --market us
./om config build --source yaml --market us --output config.us.json
./om config validate --config-path config.us.json --market us
```

生产服务建议把 `config.yaml` 和生成后的 runtime config 放在 release 目录外，例如 `/var/lib/options-monitor`：

```bash
./om config init --output /var/lib/options-monitor/config.yaml --runtime-output-dir /var/lib/options-monitor
./om config build --source yaml --market us --config-yaml /var/lib/options-monitor/config.yaml --output /var/lib/options-monitor/config.us.json
./om config build --source yaml --market hk --config-yaml /var/lib/options-monitor/config.yaml --output /var/lib/options-monitor/config.hk.json
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json
```

`config build` 会在生成的 runtime config 写入 `_generated` 元信息，记录来源路径、SHA-256 和 rebuild command。之后只要 `config.yaml` 变化，就要重新 build 对应 market 的 runtime config。旧安装的 legacy 分层源文件仍可兼容 rebuild，但该 authoring 路径已 deprecated。生产 tick 入口会检查这个指纹，避免 cron 拿陈旧 runtime config 继续跑。

不确定某个值来自哪里时，用 explain 查看覆盖链：

```bash
./om config explain --source yaml --market us --key option_positions.auto_close.enabled
./om config explain --source yaml --market us --key symbol_defaults.fetch.limit_expirations
```

### YAML 结构约定

- `accounts` 是顶层账户定义。
- `markets.us` / `markets.hk` 显式声明该市场使用哪些账户和 symbols。
- `symbols` 保持字符串列表；每个 symbol 的 DTE、strike、收益增强等个性化设置放在 `markets.<market>.overrides.<symbol>`。
- YAML 必须使用空格缩进，tab 会被拒绝；示例采用 2 个空格。
- 港股代码建议加引号，例如 `"0700.HK"`。

最小结构：

```yaml
accounts:
  lx:
    type: futu
    futu_account_id: "REPLACE_WITH_FUTU_ACCOUNT_ID"

markets:
  us:
    accounts: [lx]
    symbols:
      - NVDA
    overrides:
      NVDA:
        sell_put:
          dte: [20, 45]
          strike: [80, 120]
```

### 生成产物仍是 runtime 入口

- `config.us.json`
- `config.hk.json`

### 兼容路径

旧的分层 JSON 仍可用，主要用于已有安装、迁移和部分服务升级路径：

- `configs/system.json`
- `configs/user.common.json`（可选）
- `configs/user.us.json`
- `configs/user.hk.json`

新安装 starter：

```bash
./om config init --output config.yaml --runtime-output-dir .
```

从旧配置迁移到 `config.yaml`：

```bash
./om config migrate-yaml --output config.yaml
./om config migrate-yaml --output config.yaml --apply
```

`config init` 会同时生成 US/HK runtime JSON；已有文件时需要 `--force` 才覆盖。`migrate-yaml` 用于已有 legacy `configs/user.*.json`，默认 dry-run；只有带 `--apply` 才会写文件，默认会先备份已有 `config.yaml`。

兼容的运行时文件：

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
- `configs/examples/config.yaml.example`

### 最小配置和补充配置怎么区分？
- 最小编辑配置：`config.yaml` 里的 `accounts`、`markets.<market>.accounts` 和 `markets.<market>.symbols`
- 最小运行配置：生成后的 `config.us.json` / `config.hk.json`；期权持仓 SQLite 固定在 `<runtime_root>/output_shared/state/option_positions.sqlite3`
- 补充配置：在同一套结构上继续补 `watchdog.*`、`notifications.*`、`runtime.*`、`alert_policy.change_annual_threshold`、`intake.*`、`symbol_defaults.*`、`portfolio.source_by_account`、`feishu.*`
- 不再维护“两套心智模型”；YAML 是推荐 authoring，runtime JSON 是生成产物，legacy JSON 是兼容/迁移入口。

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

## 4) runtime config：生成后的 JSON 里需要有什么？

运行时文件通常是 `config.us.json` / `config.hk.json`，或生产 runtime root 下的同名 JSON。主路径由 `config.yaml` 生成；legacy 分层 JSON 只作为旧安装兼容和迁移入口。

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
| `./om config validate --source yaml --market us|hk` | 校验 `config.yaml` 与代码默认值合并后的配置结构、字段语义、removed/legacy 字段和数值约束 | OpenD 是否在线、环境变量是否已注入、runtime 输出是否健康 |
| `./om config validate --config-path ... --market us|hk` | 校验生成后的 runtime config、市场 schedule 时区契约和生成指纹 | OpenD 是否在线、环境变量是否已注入、runtime 输出是否健康 |
| `config_validate` | 基础 runtime config 结构校验 | OpenD 是否在线、环境变量是否已注入、生成指纹是否最新 |
| `healthcheck` | runtime config 可读、SQLite store、Feishu env readiness、OpenD readiness、option_positions bootstrap 状态 | 不负责替代主配置语义文档 |
| `runtime_status` | 只读汇总现有 runtime / OpenClaw 输出文件 | 不校验配置语义，不检查 OpenD |
| `openclaw_readiness` | 组合 `runtime_status` + `healthcheck` + 本地 openclaw 可用性 | 不替代 `config_validate` 的纯配置语义检查 |

判断规则很简单：
- YAML authoring 写得对不对，看 `./om config validate --source yaml --market us|hk`
- runtime config 是否由最新 `config.yaml` 或 legacy user config 生成，看 `./om config validate --config-path ... --market us|hk`
- 基础 runtime JSON 结构是否可读，看 `config_validate`
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
- YAML authoring 里 Covered Call 使用 `covered_call`；生成后的 runtime JSON、CSV 和 trace 仍保留内部 key `sell_call`。
- `templates.call_base.covered_call.min_strike_cost_multiplier`：Covered Call 的成本价 strike 下限倍数；模板默认 `1.02`，表示有效 `min_strike` 至少为 `avg_cost * 1.02`。
- `sell_put.min_annualized_net_return` 统一解析优先级：
  `symbol.sell_put.min_annualized_net_return` > `templates.<name>.sell_put.min_annualized_net_return` > 代码默认 `DEFAULT_MIN_ANNUALIZED_NET_RETURN(0.07)`。
- 全局流动性/价差硬过滤仅允许 3 个键：`min_open_interest`、`min_volume`、`max_spread_ratio`
- `templates.call_base.covered_call.*`：Covered Call 的通用底线

### 4.3 symbols[]：每个标的的个性化区间
你通常只需要改：
- sell_put：`min_dte/max_dte`、`min_strike/max_strike`
  - put / call 现在统一按“边界模式”规划抓取窗口，只是方向相反。
  - put 的近端边界是 `max_strike`；若只配置了 `max_strike`，抓取层会自动向下扩 `20%` 作为抓取下界。
  - `min_strike=0` 已废弃；若不想设置下界，直接省略 `min_strike`。
- covered_call（enabled 时）：`min_strike`（以及 dte 范围）
  - `avg_cost/shares` 已移除：Covered Call 仅从 holdings 自动读取。
  - `min_strike_cost_multiplier` 会用自动读取的 `avg_cost` 做硬过滤；例如 `1.02` 表示有效 `min_strike` 不低于 `avg_cost * 1.02`。
  - 若 holdings 取不到（该账户缺 holdings / 读取失败），则该账户的 Covered Call 会被跳过。
  - 抓取层现在会先为 sell_put / Covered Call 分别规划 required_data 窗口，再按相同 expiration 尽量合并到底层 OpenD 请求。
  - Covered Call 的近端边界是 `min_strike`；若只配置了 `min_strike`，抓取层会自动向上扩 `20%` 作为抓取上界。
  - 若 Covered Call 未配置任何 strike 边界，抓取层会退回到基于 `spot` 的默认窗口 `[spot*1.03, spot*1.20]`。
  - 旧的按 OTM% 定义 call 抓取窗口的配置已移除，避免与绝对价边界模式重复定义同一抓取窗口。
  - Covered Call 抓取窗口允许小幅 buffer，仅用于避免边界漏抓；扫描阶段仍严格使用原始 `min_strike/max_strike`。
- `use`: 选择使用哪些模板（例如 `["put_base","call_base"]`）
- `fetch.source`: 行情源，当前 symbol required-data 运行时仅支持 `futu`（富途数据源，经本机 OpenD 网关 + Futu API）；旧值 `opend` 仍兼容。
- `yahoo` / `yfinance` 不作为 symbol required-data 的受支持运行时来源；它们只保留给独立的事件风险数据抓取等非 OpenD fallback 场景。

### 4.4 runtime.event_risk_source：事件风险数据源

事件风险数据源由 runtime 层统一控制，扫描和 close-advice 只读取本轮 resolved event snapshot，不直接调用 Futu / yfinance。

单源配置：

```yaml
runtime:
  event_risk_source:
    provider: futu
    futu:
      host: 127.0.0.1
      port: 11111
```

多源 fallback 配置：

```yaml
runtime:
  event_risk_source:
    mode: primary_fallback
    default_provider: futu
    providers:
      futu:
        enabled: true
        role: primary
        host: 127.0.0.1
        port: 11111
      yfinance:
        enabled: true
        role: fallback
    market_rules:
      hk:
        chain: [futu]
      us:
        chain: [futu, yfinance]
```

字段口径：
- `mode=single`：只使用一个 provider；兼容旧的 `provider/source/event_risk_provider` 配置。
- `mode=primary_fallback`：按 `chain` 顺序解析，主源失败时才查备源。
- `mode=shadow`：保留为对比模式，查询链路中的多个 provider，但扫描仍消费 resolved 结果。
- `providers.<name>.enabled=false`：从链路中移除该 provider。
- `market_rules.<market>.chain`：按市场覆盖 provider chain；当前市场识别以 `.HK` 为港股，其余默认美股。

结果语义：
- `source_status=ok`：首选源成功。
- `source_status=ok_with_fallback`：首选源失败，fallback 成功。
- `source_status=stale`：所有实时源失败，但 stale cache 仍在允许窗口内。
- `source_status=error`：所有源失败且无可用 stale cache；short-vol fail-closed 时不能进入推荐。

### 4.5 portfolio：账户约束来源
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
- `option_positions.auto_close` 会使用 runtime config 的 `_generated.market` 过滤待处理 lot；`config.us.json` 只自动过期平仓 US 标的，`config.hk.json` 只自动过期平仓 HK 标的。`grace_days` 的到期 +N 天 cutoff 按标的市场本地日期计算，US 使用美东时间，HK 使用香港时间。短仓期权还必须有到期后的 OpenD spot 证明已经价外才会自动写入过期平仓；价内/平值或缺少 spot 时会进入 assignment review，等待指派/行权结果。
- `option_positions.auto_close.receipt.enabled` 默认 `true`。`./om option-positions auto-close-expired --apply` 实际写入或失败时，会按 `notify_applied` / `notify_failed` 发送回执；`notify_noop` 和 `notify_dry_run` 默认 `false`，避免无变更或 dry-run 产生噪音。回执会按账户、券商、业务日和平仓记录生成 `receipt_key`，同一业务日已确认发送的结果不会重复通知；`retry_unconfirmed` 默认 `true`，上一条回执未确认时允许后续定时/人工重跑重试。

#### 4.4.2 auto trade intake multiplier resolution

自动成交 intake 写入 open 事件前会先把 broker raw payload 里的 symbol canonicalize 到共享格式（例如 `POP` / `HK.09992` / `HK.POP260528P150000` -> `9992.HK`），再解析 multiplier。fallback 顺序固定为：

1. payload / lookup row 显式字段：`multiplier`、`contract_multiplier`、`lot_size`
2. contract metadata：本地 `output_shared/state/multiplier_cache.json`
3. cache miss 时按 listener 的 OpenD `host/port` 和 `runtime.opend_rate_limits.option_chain` 限频实时刷新并写回 cache；旧字段 `runtime.option_chain_fetch` 仍兼容

当所有来源都失败时，open deal 会进入 `unresolved_deal_ids`，并带 `retryable=true`、`missing_fields`、`multiplier_resolution.attempted_sources`、`multiplier_resolution.message` 等诊断，方便修复 OpenD 连接或补齐共享 cache 后重试。

`intake.multiplier_by_symbol`、`intake.default_multiplier_hk`、`intake.default_multiplier_us` 已退休。multiplier 是标的元数据，不属于 US/HK 策略配置；runtime config 只保留 `intake.symbol_aliases` 这类解析辅助字段。

### 4.6 notifications：推送目标
- `provider`: 通用投递器，当前主流程使用 `wechat_clawbot`
- `channel`: 投递通道，微信 ClawBot 使用 `wechat_clawbot`
- `target`: WeChat ClawBot 绑定目标，例如 `wechat:ops` 或绑定名 `ops`
- `quiet_hours_beijing`: 可选，北京时间免打扰窗口；不需要时直接省略，不要写 `null`
- `send_timeout_sec`: 可选，单次发送超时，默认 60 秒
- `wechat_clawbot_label`: 可选，绑定状态标签，默认 `default`
- `wechat_clawbot_state_dir`: 可选，绑定状态目录；默认在 `output_shared/state/channels/wechat_clawbot/<label>`
- `cash_footer_accounts` / `cash_footer_timeout_sec` / `cash_snapshot_max_age_sec`: 可选，现金摘要账户与查询参数
- `include_cash_footer`: 兼容旧 `scripts/run_pipeline.py` 的字段；多账户主流程不把它作为开关，主示例不再配置
- 不再推荐配置 `enabled` / `mode`，当前主流程不读取它们作为行为开关

微信 Clawbot 示例：

```json
{
  "notifications": {
    "provider": "wechat_clawbot",
    "channel": "wechat_clawbot",
    "target": "wechat:ops"
  }
}
```

说明：`notifications.provider=openclaw` 和 `channel=openclaw-weixin` 已移除，不再通过 OpenClaw 控制微信通知。

绑定入口：

```bash
./om channel wechat-clawbot connect --label default --name ops
./om channel wechat-clawbot list --label default
```

`connect` 会生成二维码并等待扫码确认；如果上游返回二维码图片或链接，会在状态目录写出 `login_qrcode.*`
方便远端打开或下载查看。扫码成功后，它会提示你向目标微信会话发送一条绑定文本，成功后输出可写入
`notifications.target` 的目标值，例如 `wechat:default:ops`。

入站控制试运行入口：

```bash
./om channel wechat-clawbot poll-once \
  --label default \
  --config-key us \
  --allowed-senders "wechat:<from_user_id>"
```

`poll-once` 会读取一次 iLink updates，把文本消息交给 Assistant control，并默认用同一个 ClawBot
上下文回复原消息。它是长驻 daemon 前的可验证入口；远端运行前必须显式配置 `--allowed-senders`
或在 `config.yaml` 的 `inbound.wechat_clawbot.allowed_senders` 中声明 allowlist，
不要把未授权微信用户接入 Assistant 写操作预览/确认路径。

统一通道状态入口：

```bash
./om channel status \
  --runtime-root /var/lib/options-monitor \
  --profile-path /var/lib/options-monitor/service.profile.json \
  --env-file /etc/options-monitor/options-monitor.env
```

`channel status` 会同时汇总 Feishu 与 WeChat ClawBot 的配置、service profile、allowlist 是否配置、
绑定状态和可用性；输出只返回布尔状态和脱敏路径，不返回 ClawBot `bot_token` 或 allowlist 明文。

远端长驻入口：

```bash
./om channel wechat-clawbot serve --check \
  --label default \
  --state-dir /var/lib/options-monitor/output_shared/state/channels/wechat_clawbot/default \
  --config-key us \
  --config-path /var/lib/options-monitor/config.us.json \
  --assistant-config /var/lib/options-monitor/resolved/config.assistant.json \
  --audit-db /var/lib/options-monitor/output_shared/state/inbound_control.sqlite3 \
  --allowed-senders "wechat:<from_user_id>"

./om channel wechat-clawbot serve \
  --label default \
  --state-dir /var/lib/options-monitor/output_shared/state/channels/wechat_clawbot/default \
  --config-key us \
  --config-path /var/lib/options-monitor/config.us.json \
  --assistant-config /var/lib/options-monitor/resolved/config.assistant.json \
  --audit-db /var/lib/options-monitor/output_shared/state/inbound_control.sqlite3 \
  --allowed-senders "wechat:<from_user_id>" \
  --lock-path /var/lib/options-monitor/locks/wechat-clawbot.lock
```

systemd/launchd 渲染入口：

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --include-wechat-clawbot
```

推荐把长驻服务的微信入站行为写入 `config.yaml`，再生成 `config.assistant.json`：

```yaml
inbound:
  wechat_clawbot:
    label: default
    allowed_senders: "wechat:<from_user_id>"
    reply_enabled: true
    max_reply_chars: 3500
    poll_interval_sec: 3.0
    timeout_sec: 20
```

如果需要临时覆盖 allowlist，可以在 `service render` 时传
`--wechat-clawbot-allowed-senders "wechat:<from_user_id>"`；没有通配默认值。来自 `config.yaml`
的 allowlist 不会在 `service.profile.json` 中重复明文保存，profile 只记录是否已配置和来源。

底层排障入口：

```bash
./om channel wechat-clawbot qrcode --label default
./om channel wechat-clawbot qr-status --label default
./om channel wechat-clawbot bind --label default --name ops --match-text "bind ops"
```

### 4.7 schedule：监控时间窗口
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

### 4.8 runtime：超时（线上稳定）
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

### 4.9 alert_policy：提醒变化阈值
- `change_annual_threshold`：年化变化达到该阈值才写入 changes
- `sell_put`：Sell Put 候选评级阈值（可选；缺省即下表默认值）
  - `high_annual`：年化净收益≥该值且 `high_spread_max` 同时满足，归为「优先」（默认 0.20）
  - `high_spread_max`：买卖价差比≤该值，配合 `high_annual` 触发「优先」（默认 0.20）
  - `medium_annual`：年化净收益≥该值，归为「可考虑」（默认 0.12）
- `covered_call`：Covered Call 候选评级阈值（可选；缺省即下表默认值）
  - `high_annual`：年化权利金回报≥该值且 `high_total` 同时满足，归为「优先」（默认 0.10）
  - `high_total`：行权情形下总收益≥该值，配合 `high_annual` 触发「优先」（默认 0.15）
  - `medium_annual`：年化权利金回报≥该值，归为「可考虑」（默认 0.06）

不写 `sell_put` / `covered_call` 时使用上述默认值，与历史硬编码行为一致。完整示例：

```yaml
alert_policy:
  change_annual_threshold: 0.02
  sell_put:
    high_annual: 0.20
    high_spread_max: 0.20
    medium_annual: 0.12
  covered_call:
    high_annual: 0.10
    high_total: 0.15
    medium_annual: 0.06
```

### 4.10 close_advice：平仓建议
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
- 独立 close-advice 命令：默认写到 `output_shared/reports/close_advice.csv` / `output_shared/reports/close_advice.txt`
- 统一 tick 运行：按账户写到 `output_runs/<run_id>/accounts/<account>/close_advice.csv|txt`

### 4.11 手续费：内置规则
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
cat /home/node/.openclaw/workspace/options-monitor-prod/output_shared/state/last_run.json
cat /home/node/.openclaw/workspace/options-monitor-prod/<report_dir>/symbols_notification.txt  # 默认 report_dir=output_shared/reports
```
