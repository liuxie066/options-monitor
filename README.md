# Options Monitor

> 状态：可用（持续迭代）— 里程碑见 VERSION.md（首个可用里程碑：2026-03-22）

一个期权监控与提醒工具，覆盖 **美股/港股**，主要用于卖方策略扫描：

- Sell Put（现金担保）
- Sell Call（覆盖式）

数据源策略：

- 主要：富途 OpenD（US/HK）
- 降级：美股在 OpenD 不可用时可选 Yahoo/yfinance（见 `config.us.json:fetch_policy`）
- 港股：默认 OpenD-only（见 `config.hk.json:fetch_policy`）

核心能力：

- 抓取期权链与必要字段（含 IV/Delta 等；缺失会明确提示）
- 按规则筛选候选并计算（含手续费）净收益/年化等指标
- 生成摘要/提醒文本（支持飞书通知、多账户合并输出）

## 当前范围

当前版本适合：
- 验证策略扫描规则
- 做日常候选筛选
- 输出 Sell Put / Sell Call 的候选与提醒

当前版本不适合：
- 作为长期严肃生产级主数据源
- 依赖 Yahoo 数据做高精度实时交易决策
- 替代人工风险控制

## 安装

推荐用项目自带脚本启动（会自动创建虚拟环境并安装依赖）：

```bash
./run_watchlist.sh
```

如需手动安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

> 配置入口默认是 `config.us.json`（可用环境变量 `OPTIONS_MONITOR_CONFIG` 覆盖）。

## 配置

- 推荐入口：`config.us.json`（美股）/ `config.hk.json`（港股）
- `config.json`：历史兼容入口（不再推荐作为唯一入口）
- `scripts/`：抓取、扫描、提醒与统一入口脚本
- `output/raw/`：原始 JSON
- `output/parsed/`：标准化 CSV
- `output/reports/`：候选、提醒与摘要输出

## 消息 Intake（成交提醒 → option_positions）

- 解析成交/手工输入消息：`scripts/parse_option_message.py`
- 解析 + 写入（默认 dry-run）：`scripts/option_intake.py`

Intake 可配置项（见 `config.us.json:intake` / `config.hk.json:intake`，或历史 `config.json:intake`）：
- `symbol_aliases`: 中文标的名 → 代码（例如 中海油 → 0883.HK）
- `multiplier_by_symbol`: 合约乘数（例如 0883.HK → 1000）
- `default_multiplier_hk` / `default_multiplier_us`

## Watchlist 管理（监控标的：查看/新增/删除/编辑）

配置文件：`config.us.json` / `config.hk.json` 的 `symbols[]`（历史：`config.json`）

```bash
# 查看当前监控标的
./.venv/bin/python scripts/watchlist.py list

# 新增标的（示例：只监控 sell put；默认所有账户都跑）
./.venv/bin/python scripts/watchlist.py add TSLA --put --use put_base --limit-exp 8

# 新增标的（只让某些账户跑）
./.venv/bin/python scripts/watchlist.py add AAPL --put --accounts lx

# 删除标的
./.venv/bin/python scripts/watchlist.py rm TSLA

# 编辑标的（用 --set 打补丁，支持重复）
./.venv/bin/python scripts/watchlist.py edit NVDA --set sell_put.min_strike=145 --set sell_put.max_strike=160
./.venv/bin/python scripts/watchlist.py edit NVDA --set sell_call.enabled=false

# 也可以给已有标的指定账户（默认不写 accounts = 两个账户都跑）
./.venv/bin/python scripts/watchlist.py edit GOOGL --set accounts=["lx","sy"]
./.venv/bin/python scripts/watchlist.py edit FUTU --set accounts=["lx"]
```

## 当前主要脚本

- `scripts/fetch_market_data.py`
- `scripts/scan_sell_put.py`
- `scripts/scan_sell_call.py`
- `scripts/render_sell_put_alerts.py`
- `scripts/render_sell_call_alerts.py`
- `scripts/alert_engine.py`
- `scripts/scan_scheduler.py`
- `scripts/run_pipeline.py`

## 主要输出

- `output/reports/symbols_summary.csv` / `symbols_summary.txt`
- `output/reports/symbols_digest.txt`
- `output/reports/symbols_alerts.txt`
- `output/reports/symbols_changes.txt`
- `output/reports/symbols_notification.txt`

