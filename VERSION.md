# options-monitor — Milestones

> 说明：本仓库不提交真实运行配置（仅保留 `config.example.*.json` 示例）。

## 2026-03-28 (phase: 配置示例化 + 文档对齐)

- 仓库只保留示例配置：`config.example.us.json` / `config.example.hk.json`。
- 真实运行配置建议使用本地文件：`config.local.us.json` / `config.local.hk.json`（已加入 `.gitignore`）。
- `run_watchlist.sh` 默认读取 `config.local.us.json`，并在缺失时提示从示例复制。

## 2026-03-22 (phase: 线上可用版)

- 生产入口固定为单命令：`scripts/send_if_needed.py`（scheduler → pipeline → send → mark-notified）。
- Cron 运行：交易时段监控；北京时间 02:00 之后改为 60 分钟间隔；非交易时段不监控。
- 通知：适配飞书私聊，Put/Call 分组，内容极简。
- 可靠性：并发锁、陈旧锁清理、`last_run.json` 记录 duration_ms、运行阶段与原因。
- 风控口径：base=CNY；USD/HKD 作为等值展示；FX 缓存与可追溯。
- 表接入：通过飞书 Bitable 读取 holdings/option_positions；并对 Bitable DateTime 字段做毫秒时间戳归一化（日期字符串→12:00 UTC）。
