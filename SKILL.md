---
name: options-monitor
description: |
  期权监控与提醒（sell put / sell call / 平仓建议），支持多账户（lx/sy）分开计算约束，但合并输出。

  代码仓库：/home/node/.openclaw/workspace/options-monitor
  上游仓库：https://github.com/liuxie85/options-monitor

  主要能力：
  - 生产 tick：按交易时段扫描，合并发送 Feishu 提醒
  - 卖 Put 担保占用与剩余现金查询：统一折算到 CNY
  - 平仓建议：评估 open short put/call 的已锁定收益、剩余 DTE、剩余收益年化，生成买回提醒
  - watchlist 管理：查看/新增/删除/编辑监控标的（symbols[].accounts 可选，不写=两账户都跑）
  - option_positions 维护：查看/新增/编辑/关闭（short put 自动算 cash_secured_amount，写操作建议先 dry-run）
---

# options-monitor

> 说明：OpenClaw 实际启用的 Skill 入口文档位于：`~/.openclaw/workspace/skills/options-monitor/SKILL.md`。
> 本仓库内的这份 `SKILL.md` 用于让项目自描述、便于代码同行阅读。

## 入口命令（白名单）

### 1) 生产 tick（多账户合并提醒）

```bash
cd /home/node/.openclaw/workspace/options-monitor
./.venv/bin/python scripts/send_if_needed_multi.py --config config.us.json --accounts lx sy
```

### 2) 查询卖 Put 担保占用 / 剩余现金（按账户，统一折算 CNY）

```bash
./.venv/bin/python scripts/cli/query_sell_put_cash_cli.py --market 富途 --account lx
./.venv/bin/python scripts/cli/query_sell_put_cash_cli.py --market 富途 --account sy
```

### 3) watchlist 管理（监控标的）

```bash
./.venv/bin/python scripts/watchlist.py list
./.venv/bin/python scripts/watchlist.py add TCOM --put
./.venv/bin/python scripts/watchlist.py edit TCOM --set sell_put.max_strike=45
./.venv/bin/python scripts/watchlist.py rm TCOM
```

### 4) option_positions 表维护（飞书多维表）

```bash
# 查看
./.venv/bin/python scripts/option_positions.py list --market 富途 --account lx --status open

# 新增 short put（自动算 cash_secured_amount = strike * multiplier * contracts）
./.venv/bin/python scripts/option_positions.py add \
  --account lx --symbol 0700.HK --option-type put --side short --contracts 1 \
  --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --dry-run
```

### 5) 平仓建议（只生成报告/提醒文本，不下单）

```bash
./.venv/bin/python scripts/close_advice.py \
  --config config.us.json \
  --context output/state/option_positions_context.json \
  --required-data-root output \
  --output-dir output/reports
```

多账户 tick 中，只有配置 `close_advice.enabled=true` 时才会自动生成 `close_advice.csv` / `close_advice.txt`，并把强/中两档追加到账户通知。

## 计算口径（摘要）

- **short put 担保占用（原币）**：`cash_secured_amount = strike * multiplier * contracts`，并以 `currency` 指定的币种存入表。
- **统一到 CNY 的风控口径**：用 `USDCNY/HKDCNY` 折算后汇总到 `cash_secured_total_cny`，再与 holdings 的可用资金池做差。
- **covered call 覆盖**：用持仓股数减去已锁股数，按 100 股/张折算可覆盖张数。
- **平仓建议**：`capture_ratio = (premium - close_mid) / premium`；short put 的剩余收益年化约为 `close_mid / strike / dte * 365`，short call 约为 `close_mid / spot / dte * 365`。
- **平仓建议强度**：强/中/弱/可选由已锁定收益、剩余 DTE、剩余收益年化共同决定；默认只通知 `strong` / `medium`。

## 重要约束

- 数据缺失时必须明确提示缺失，不允许脑补。
- 平仓建议必须使用 option_positions 表的 `premium` 或 `note` 中的 `premium_per_share`；不要从历史行情反推开仓权利金。
- 平仓建议只提醒，不自动下单、不写回 option_positions。
- 涉及写入飞书（新增/编辑/关闭仓位）建议先 `--dry-run`，确认字段无误再执行。

## 常用检查

```bash
python3 -m pytest tests/test_close_advice_domain.py tests/test_close_advice_runner.py tests/test_option_positions_context_partial_close.py
python3 -m pytest tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py
```
