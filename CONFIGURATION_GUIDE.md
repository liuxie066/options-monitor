# options-monitor 配置与表结构说明（实战版）

> 目标：你只要维护：
> - `options-monitor/config.us.json 或 options-monitor/config.hk.json`（策略与监控）
> - 飞书 Bitable 的两张表：`holdings`、`option_positions`（账户约束）
> - （可选）Feishu App 凭证（用于程序读取 Bitable）

---

## 1) 本项目需要哪些外部“表”（Bitable）？

目前本项目会通过 `portfolio-management/config.json` 读取飞书 Bitable（这是 PM 凭证配置，不是 options-monitor 运行入口配置）：
- `holdings`：现金与股票持仓（用于 base 现金、shares、avg_cost）
- `option_positions`：已卖出期权占用（用于：
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

## 4) options-monitor/config.us.json 或 options-monitor/config.hk.json：你需要配置什么？

文件：`options-monitor/config.us.json 或 options-monitor/config.hk.json`

### 4.1 templates：通用底线（复用）
- `templates.put_base.sell_put.min_annualized_net_return`：全局 put 最低年化（例如 0.10）
- `sell_put.min_annualized_net_return` 统一解析优先级：
  `symbol.sell_put.min_annualized_net_return` > `templates.<name>.sell_put.min_annualized_net_return` > 代码默认 `DEFAULT_MIN_ANNUALIZED_NET_RETURN(0.07)`。
- `min_open_interest / min_volume / max_spread_ratio`：流动性与可成交性底线
- `templates.call_base.sell_call.*`：call 的通用底线

### 4.2 symbols[]：每个标的的个性化区间
你通常只需要改：
- sell_put：`min_dte/max_dte`、`min_strike/max_strike`
- sell_call（enabled 时）：`min_strike`（以及 dte 范围）
  - `avg_cost/shares` 已移除：sell_call 仅从 holdings 自动读取。
  - 若 holdings 取不到（该账户缺 holdings / 读取失败），则该账户的 sell_call 会被跳过。
- `use`: 选择使用哪些模板（例如 `["put_base","call_base"]`）

### 4.3 portfolio：账户约束来源
- `pm_config`: 指向 `../portfolio-management/config.json`（仅 PM 凭证配置，不是 OM 运行入口）
- `market`: 用来过滤两张表（例如 `富途`）
- `account`: 用来过滤两张表（例如 `lx`）
- `base_currency`: 当前策略口径（CNY）

### 4.4 notifications：推送目标
- `enabled`: true/false
- `channel`: `feishu`
- `target`: `user:open_id` 或 `chat:chat_id`
- `mode`: 当前为 `change_or_high`

### 4.5 schedule：频率（你已定制）
- 非交易时段：不监控
- 交易时段：北京时间 2 点前密，2 点后稀

### 4.6 runtime：超时（线上稳定）
- `symbol_timeout_sec`：单标的 fetch/scan 超时
- `portfolio_timeout_sec`：读取 holdings/positions 超时

### 4.7 alert_policy：提醒分级门槛
- `sell_put_high_return`、`sell_call_high_return` 等

---

## 5) Feishu App 凭证（AppID/AppSecret）到底放哪？

### 当前实现（已在用）
- `portfolio-management/config.json` 内的 `feishu.app_id/app_secret` 用于获取 tenant_access_token，再读取 Bitable（这是 PM 凭证配置，不是 options-monitor 运行入口配置）。
- options-monitor 通过 `portfolio.pm_config` 引用这一份配置。

### 推荐改进（更安全/更自洽）
- 在 options-monitor 内部放 `secrets/feishu.json`（不进 git），由 options-monitor 自己读取。
- 当前仓库已加入 `.gitignore`（忽略 secrets/ 与 output/）。

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
