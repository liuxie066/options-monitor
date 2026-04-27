# options-monitor 配置与表结构说明（实战版）

> 目标：你只要维护：
> - 仓内 `config.us.json` / `config.hk.json`（策略与监控）
> - `portfolio.data_config`（最小配置下只需要 SQLite `option_positions` 路径）
> - Feishu App 凭证和 Bitable 配置是可选项，只在 `holdings` / `external_holdings` 数据源场景需要

---

## 0) 最终保留哪几个配置文件？

### 你实际维护的运行时文件
- `config.us.json`
- `config.hk.json`
- `secrets/portfolio.sqlite.json`
- `secrets/portfolio.external_holdings.json`（可选，只在 external_holdings 账号场景需要）
- `secrets/portfolio.feishu.json`（可选，只在 `holdings` / `external_holdings` 数据源场景需要）

### 仓库里保留的模板文件
- `configs/examples/config.example.us.json`
- `configs/examples/config.example.hk.json`
- `configs/examples/portfolio.sqlite.example.json`
- `configs/examples/portfolio.external_holdings.example.json`
- `configs/examples/portfolio.feishu.example.json`

### 最小配置和补充配置怎么区分？
- 最小配置：`config.us.json` / `config.hk.json` + `secrets/portfolio.sqlite.json`
- 补充配置：在同一套结构上继续补 `notifications.*`、`runtime.*`、`alert_policy.change_annual_threshold`、`intake.*`、`portfolio.source_by_account`、`feishu.*`
- 不再维护“两套 schema”或“两份不同风格文档”；只有一套结构，只是填写程度不同。

---

## 1) 本项目需要哪些外部“表”（Bitable）？

目前本项目通过 `portfolio.data_config` 指向本地 portfolio 配置文件。
最小配置下它只需要提供 SQLite `option_positions` 路径；如果你要启用 Feishu holdings 数据源，再在同一个文件里补 `feishu` 配置。
- `holdings`：可选主数据源，提供现金与股票持仓（用于 base 现金、shares、avg_cost）
- `option_positions`：SQLite 主存储，提供已卖出期权占用（用于：
  - covered call 锁股数 `locked_shares_by_symbol`
  - cash-secured put 占用 `cash_secured_by_symbol`
)

**你需要给我的信息（不含密钥）**：
- 两张表的 Bitable 链接（或 app_token/table_id）
- 两张表里字段名是否与下文一致（截图/字段列表即可）

---

## 2) holdings 表：字段要求（fetch_portfolio_context.py）

脚本：`scripts/fetch_portfolio_context.py`

### 2.1 过滤逻辑
- 读取全表后按两列过滤：
  - `market`：要求该字段的字符串 **包含** config 里传入的 market（容错匹配）
  - `account`：若传入 account，则要求 **完全相等**

> 注意：holdings 的 market 是“包含匹配”，option_positions 是“完全相等”（见下文）。

### 2.2 必需字段（字段名必须一致）
通用：
- `asset_type`：字符串，至少需要支持：
  - `cash`
  - `us_stock`
- `market`：字符串（如：`富途`）
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

## 3) option_positions 表：字段要求（fetch_option_positions_context.py）

脚本：`scripts/fetch_option_positions_context.py`

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
- `underlying_share_locked`（推荐字段名）：covered call 锁定股数
  - 兼容字段：`underlying_shares_locked`
  - 如果为空且是 short call，会按 `contracts * 100` 推算
- `cash_secured_amount`：short put 的现金担保占用（美元数值）

备注字段（可选）：
- `note`：可写 `key=value`；脚本支持 `status/option_type/side` 从 note 里解析

### 3.3 输出给监控系统的关键字段
- `locked_shares_by_symbol`：用于 covered call 可卖张数 = (shares - locked)/100
- `cash_secured_by_symbol`：用于卖 put 的“已占用担保现金”

---

## 4) config.us.json 或 config.hk.json：你需要配置什么？

安装版默认文件：`config.us.json` 或 `config.hk.json`

### 4.0 先看最小配置：哪些字段一定要有？

#### runtime config 最小必需
- `accounts`
- `trade_intake.account_mapping.futu`
- `templates`
- `portfolio.data_config`
- `portfolio.broker`
- `portfolio.account`
- `portfolio.source`
- `portfolio.base_currency`
- `schedule`
- `symbols`

#### data_config 最小必需
- `option_positions.sqlite_path`

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

### 4.1 accounts：账户列表
- `accounts`: 多账户运行和辅助脚本的默认账户列表，例如 `["user1"]`。
- 脚本命令行显式传 `--accounts` 时，以命令行为准。
- `notifications.cash_footer_accounts` 仅在你要指定“部分账户带现金 footer”时才配置；未配置时会回退到 `accounts`，避免与账户列表重复维护。

### 4.2 templates：通用底线（复用）
- `templates.put_base.sell_put.min_annualized_net_return`：全局 put 最低年化（例如 0.10）
- `templates.*.*.min_net_income`：全局最低单笔净收益，统一按 CNY 配置；运行时会按标的币种换算为 USD/HKD 后传给扫描器。
- `sell_put.min_annualized_net_return` 统一解析优先级：
  `symbol.sell_put.min_annualized_net_return` > `templates.<name>.sell_put.min_annualized_net_return` > 代码默认 `DEFAULT_MIN_ANNUALIZED_NET_RETURN(0.07)`。
- 全局流动性/价差硬过滤仅允许 3 个键：`min_open_interest`、`min_volume`、`max_spread_ratio`
- `templates.call_base.sell_call.*`：call 的通用底线

### 4.3 symbols[]：每个标的的个性化区间
你通常只需要改：
- sell_put：`min_dte/max_dte`、`min_strike/max_strike`
- sell_call（enabled 时）：`min_strike`（以及 dte 范围）
  - `avg_cost/shares` 已移除：sell_call 仅从 holdings 自动读取。
  - 若 holdings 取不到（该账户缺 holdings / 读取失败），则该账户的 sell_call 会被跳过。
- `use`: 选择使用哪些模板（例如 `["put_base","call_base"]`）
- `fetch.source`: 行情源，新配置建议使用 `futu`（富途数据源，经本机 OpenD 网关 + Futu API）或 `yahoo`；旧值 `opend` 仍兼容。

### 4.4 portfolio：账户约束来源
- `data_config`: 最小配置建议指向 `secrets/portfolio.sqlite.json`，只负责 `option_positions.sqlite_path`
- `data_config`: 持仓/SQLite/Feishu 数据配置路径
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

### 4.5 notifications：推送目标
- `channel`: `feishu`，或本机 `openclaw` 已支持的其他通道
- `target`: `user:open_id` 或 `chat:chat_id`
- `quiet_hours_beijing`: 可选，北京时间免打扰窗口；不需要时直接省略，不要写 `null`
- `cash_footer_accounts` / `cash_footer_timeout_sec` / `cash_snapshot_max_age_sec`: 可选，现金摘要账户与查询参数
- `include_cash_footer`: 兼容旧 `scripts/run_pipeline.py` 的字段；多账户主流程不把它作为开关，主示例不再配置
- 不再推荐配置 `enabled` / `mode`，当前主流程不读取它们作为行为开关

### 4.6 schedule：监控时间窗口
- 非交易日 / 非交易时段：不监控、不通知。
- 交易时段：开盘后 30 分钟通知一次，之后每小时通知一次，收盘前 10 分钟通知一次。
- 港股午休等中场休市可用 `market_break_start` / `market_break_end` 配置，休市窗口内会跳过。
- 可调字段：`first_notify_after_open_min`、`notify_interval_min`、`final_notify_before_close_min`。

### 4.7 runtime：超时（线上稳定）
- `symbol_timeout_sec`：单标的 fetch/scan 超时
- `portfolio_timeout_sec`：读取 holdings/positions 超时

### 4.8 alert_policy：提醒变化阈值
- `change_annual_threshold`：年化变化达到该阈值才写入 changes

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
    "max_spread_ratio": 0.4,
    "strong_remaining_annualized_max": 0.08,
    "medium_remaining_annualized_max": 0.12
  }
}
```

默认输出文件：
- 单账户：`output/reports/close_advice.csv` / `output/reports/close_advice.txt`
- 多账户：`output_runs/<run_id>/accounts/<account>/close_advice.csv|txt`

### 4.10 手续费：内置规则
- `fees` 已不再支持配置。
- 当前默认内置规则：
  - US：富途美股期权完整手续费口径
  - HK：富途港股期权完整手续费口径
- 如果配置文件里仍带 `fees`，`validate_config` 会直接报错。

---

## 5) `portfolio.data_config` / Feishu App 凭证到底放哪？

### 最小方式（新部署）
- 从 `configs/examples/portfolio.sqlite.example.json` 复制到 `secrets/portfolio.sqlite.json`。
- 在 `secrets/portfolio.sqlite.json` 内填写或保留 `option_positions.sqlite_path`。
- 在 `config.us.json` / `config.hk.json` 内保持 `portfolio.data_config = "secrets/portfolio.sqlite.json"`。

示例：

```json
{
  "option_positions": {
    "sqlite_path": "output_shared/state/option_positions.sqlite3"
  }
}
```

### 可选方式（启用 Feishu holdings 数据源）
- 从 `configs/examples/portfolio.feishu.example.json` 复制到本地安全路径，例如 `secrets/portfolio.feishu.json` 或 `/opt/options-monitor/secrets/portfolio.feishu.json`。
- 在该文件内填写 `feishu.app_id/app_secret` 和 `tables.holdings` / `tables.option_positions`。
- 在 `config.us.json` / `config.hk.json` 内把 `portfolio.data_config` 指向该文件。

### 可选方式（增加 external_holdings 账号）
- 先执行：

```bash
./om-agent add-account --market us --account-label ext1 --account-type external_holdings --holdings-account "Feishu EXT"
```

- 再从 `configs/examples/portfolio.external_holdings.example.json` 复制到本地安全路径，例如 `secrets/portfolio.external_holdings.json`。
- 在该文件内填写：
  - `option_positions.sqlite_path`
  - `feishu.app_id`
  - `feishu.app_secret`
  - `feishu.tables.holdings`
- 然后把 `config.us.json` / `config.hk.json` 里的 `portfolio.data_config` 指向这个本地文件。

示例：

```json
{
  "option_positions": {
    "sqlite_path": "output_shared/state/option_positions.sqlite3"
  },
  "feishu": {
    "app_id": "cli_YOUR_APP_ID",
    "app_secret": "YOUR_APP_SECRET",
    "tables": {
      "holdings": "app_token/table_id"
    }
  }
}
```

### 外部数据配置（旧部署也适用）
- 如果你已经在仓外维护数据配置 JSON，也可以直接把 `portfolio.data_config` 指向该文件。
- 或设置环境变量 `OM_DATA_CONFIG=/absolute/path/to/portfolio.sqlite.json`。

示例：

```json
{
  "feishu": {
    "app_id": "cli_YOUR_APP_ID",
    "app_secret": "YOUR_APP_SECRET",
    "tables": {
      "holdings": "app_token/table_id",
      "option_positions": "app_token/table_id"
    }
  }
}
```

当前仓库已加入 `.gitignore`（忽略 `secrets/` 与 `output/`）。

> 注意：不要在聊天里发送 app_secret。

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
