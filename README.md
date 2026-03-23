# Options Monitor

> 状态：线上可用版（2026-03-22）— 里程碑见 VERSION.md

一个基于 Yahoo Finance / yfinance 的美股期权监控工具，当前聚焦两类卖方策略：

- Sell Put
- Sell Call

目标是提供一个可重复运行的最小版本，用于：

- 抓取美股期权链
- 基于规则筛选候选
- 计算扣手续费后的净收益指标
- 生成提醒文本和策略摘要

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

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 推荐运行方式

推荐用项目自带脚本（会自动处理虚拟环境与依赖）：

```bash
./run_watchlist.sh
```

这会自动完成：
1. 抓取标的数据与期权链
2. 扫描 Sell Put 候选
3. 扫描 Sell Call 候选
4. 生成提醒文本
5. 生成策略摘要

## 主要目录

- `config.json`：唯一配置入口（templates + symbols）
- `scripts/`：抓取、扫描、提醒与统一入口脚本
- `output/raw/`：原始 JSON
- `output/parsed/`：标准化 CSV
- `output/reports/`：候选、提醒与摘要输出

## Watchlist 管理（监控标的：查看/新增/删除/编辑）

配置文件：`config.json` 的 `symbols[]`

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

