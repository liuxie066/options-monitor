# Tool Reference

这份文档只回答两件事：

1. `om-agent` 目前有哪些公开工具
2. 它们和人工 CLI `om` 的关系是什么

如果你只想跑产品，先看根目录 [README.md](../README.md)。

---

## 1. 两套入口的区别

| 入口 | 面向对象 | 典型用途 |
|---|---|---|
| `./om` | 人工操作 | 手动跑 pipeline、分阶段运行、命令行查询 |
| `./om-agent` | 程序 / Agent | JSON manifest、结构化 tool 调用 |

一句话：

- `om` 是人类 CLI
- `om-agent` 是程序化工具入口

---

## 2. 如何查看工具清单

```bash
./om-agent spec
```

它会输出当前环境下可用的工具 manifest。

注意：
- `spec` 不是绝对静态的
- 某些默认值会受环境变量影响，例如写工具门禁

---

## 3. 常见调用方式

```bash
./om-agent run --tool <tool-name> --input-json '<json>'
```

也支持：

```bash
./om-agent run --tool <tool-name> --input-file payload.json
```

`--input-file` 会覆盖 `--input-json`。

---

## 4. Tool 与 `om` CLI 的关系

有些能力同时存在于：

- `om-agent` 的 tool
- `om` 的命令行入口

但名字不一定一样。

### 常见映射

| `om-agent` tool | `om` / CLI 对应能力 |
|---|---|
| `healthcheck` | `./om healthcheck` |
| `version_check` | `./om version` |
| `config_validate` | `./om config validate` |
| `scheduler_status` | `./om scheduler` 的只读判定部分 |
| `scan_opportunities` | `./om scan` / `./om scan-pipeline` |
| `preview_notification` | `./om notify preview` |
| `get_close_advice` | `./om close-advice` |
| `query_cash_headroom` | `./om sell-put-cash` |
| `monthly_income_report` | `scripts/option_positions_report.py monthly-income` |
| `option_positions_read` | `scripts/option_positions.py list/events/history/inspect` 的只读部分 |

说明：
- `om-agent` 更适合给程序调
- `om` 更适合人工操作

---

## 5. 当前公开工具列表

## 5.1 `healthcheck`

用途：
- 校验 runtime config
- 检查账户路径
- 检查 OpenD / SQLite / 通知前置条件

示例：

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

---

## 5.2 `version_check`

用途：
- 检查本地 `VERSION` 与 git 远端发布 tag
- 不运行监控流程

示例：

```bash
./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'
```

---

## 5.3 `config_validate`

用途：
- 只校验 runtime config
- 不检查 OpenD
- 不运行 pipeline

示例：

```bash
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

---

## 5.4 `scheduler_status`

用途：
- 读取现有 scheduler state
- 返回当前调度判定、下次运行时间、是否处于通知窗口
- 不执行 `run-if-due`
- 不写 `mark-scanned` / `mark-notified`

示例：

```bash
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"user1"}'
```

---

## 5.5 `scan_opportunities`

用途：
- 跑扫描流程
- 返回候选摘要

示例：

```bash
./om-agent run --tool scan_opportunities --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":3}'
```

---

## 5.6 `query_cash_headroom`

用途：
- 查询 Sell Put 现金占用与余量

示例：

```bash
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"user1"}'
```

注意：
- 公开输入优先用 `broker`
- `market` 仍作为兼容别名存在

---

## 5.7 `monthly_income_report`

用途：
- 读取本地 option positions
- 返回月度已实现收益和开仓权利金收入统计
- 默认只返回 summary；`include_rows=true` 时返回明细

示例：

```bash
./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"user1","month":"2026-04"}'
```

---

## 5.8 `option_positions_read`

用途：
- `action=list`：读取 position lots
- `action=events`：读取 canonical trade events
- `action=history`：读取单个 lot 的事件链
- `action=inspect`：读取投影诊断状态

示例：

```bash
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"user1","status":"open"}'
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"history","record_id":"rec_xxx"}'
```

注意：
- 这个工具只开放读和诊断动作
- `add` / `buy-close` / `void-event` / `adjust-lot` / `rebuild` 不在此工具中开放

---

## 5.9 `get_portfolio_context`

用途：
- 获取账户持仓 / 现金 context

示例：

```bash
./om-agent run --tool get_portfolio_context --input-json '{"config_key":"us","account":"user1"}'
```

---

## 5.10 `prepare_close_advice_inputs`

用途：
- 预先刷新 close advice 依赖的本地输入

通常与 `close_advice` 搭配使用。

---

## 5.11 `close_advice`

用途：
- 基于本地 context 和 quotes 构建平仓建议

示例：

```bash
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

---

## 5.12 `get_close_advice`

用途：
- 一次性执行 close advice 推荐路径

示例：

```bash
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

这是更推荐的 Agent 入口。

---

## 5.13 `manage_symbols`

用途：
- 读取或修改 `symbols[]`

示例：

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

注意：
- `list` 永远是只读
- 真正写操作需要：
  - `OM_AGENT_ENABLE_WRITE_TOOLS=true`
  - `confirm=true`

---

## 5.14 `preview_notification`

用途：
- 只生成通知内容，不发送

示例：

```bash
./om-agent run --tool preview_notification --input-json '{"alerts_path":"output/reports/symbols_alerts.txt","changes_path":"output/reports/symbols_changes.txt","account_label":"user1"}'
```

---

## 6. 人工 CLI：版本检查

`./om version` 仍然保留为人工 CLI 能力。Agent 使用 `version_check`，二者读取同一个本地 `VERSION` 和远端 `v*` tags。

示例：

```bash
./om version
```

---

## 7. WebUI 能调用哪些工具

这点要特别说明：

> `om-agent` 有很多工具，但 **WebUI 并不会全部开放。**

当前 `scripts/webui/server.py` 里，`/api/tools/run` 只允许：

- `healthcheck`
- `scan_opportunities`
- `get_close_advice`

所以：
- Tool Reference 是面向 **agent/tool 调用方** 的
- 不是 WebUI 可调用范围的完整镜像

---

## 8. 字段口径

### 优先使用
- `broker`

### 兼容存在
- `market`

对于 agent 工具输入，文档和新调用建议优先使用 `broker`；旧调用仍可能继续接受 `market`。

---

## 9. 相关文档

- Agent 合同：[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md)
- 快速开始：[`GETTING_STARTED.md`](GETTING_STARTED.md)
- 配置说明：[`../CONFIGURATION_GUIDE.md`](../CONFIGURATION_GUIDE.md)
