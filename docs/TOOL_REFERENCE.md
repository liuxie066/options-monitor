# Tool Reference

本文说明如何发现和安全调用 `om-agent` Tool Gateway。它不手工复制每个工具的完整 schema。

运行时权威：

```bash
./om-agent spec
```

源码权威：

- `src/application/agent_tool_registry.py`
- `src/application/agent_tools/*`
- `src/application/tool_execution.py`

## `om` 与 `om-agent`

| 入口 | 受众 | 作用 |
|---|---|---|
| `./om` | 人工操作者 | 配置、扫描、账本、研究、服务和运维 workflow |
| `./om-agent` | 外部 agent、脚本、结构化集成 | JSON manifest 与单工具 JSON envelope |
| `./om assistant` / `./om copilot` | 消息入口与 OM Copilot | Control / Copilot，不属于 Tool Gateway |

`om-agent` 不维护对话状态，不负责多步规划，也不是自动交易 Agent。

## Manifest

查看完整 manifest：

```bash
./om-agent spec
```

只列工具名：

```bash
./om-agent spec | jq -r '.tools[].name'
```

查看一个工具的 schema、示例和风险：

```bash
./om-agent spec |
  jq '.tools[] | select(.name == "runtime_status")'
```

每个工具至少声明：

| 字段 | 含义 |
|---|---|
| `name` | 稳定工具名 |
| `input_json_schema` | 实际输入约束 |
| `read_only` | 是否修改产品事实或配置 |
| `risk_level` | 当前执行风险分类 |
| `side_effects` | 可能物化的本地或外部结果 |
| `requires_confirm` | 是否需要确认语义 |
| `requires_env` | 依赖的环境 gate |
| `safe_default_input` | manifest 声明的安全默认值 |
| `output_contract` | 结果中可见事实、freshness 和证据形态 |

注意：

- `read_only=true` 不等于“绝不写任何本地文件”。部分工具不修改产品状态，但会物化 report/cache。
- `defaults.write_tools_enabled` 反映当前环境是否允许写工具；工具自身 metadata 不会因开关而消失。
- 不要假设所有写命令都有同一套 `--apply` / `--confirm` 参数；以单个 manifest 为准。

## 当前工具分类

以下分类基于当前 registry，完整 schema 仍以 `spec` 为准。

### 诊断与运行

- `healthcheck`
- `config_validate`
- `runtime_status`
- `runtime_runs`
- `runtime_logs`
- `scheduler_status`
- `operation_timeline`
- `version_check`
- `version_update`

### 候选与 symbol

- `scan_opportunities`
- `candidate_rank_explain`
- `candidate_filter_explain`
- `symbol_resolve`
- `symbol_config_read`
- `manage_symbols`

### 分析与收益

- `analysis_catalog`
- `analysis_query`
- `option_performance_report`
- `portfolio_pnl_bridge`
- `portfolio_cash_bridge`

### 持仓、现金与组合

- `option_positions_read`
- `query_cash_headroom`
- `get_portfolio_context`
- `portfolio_query`

### Close Advice

- `prepare_close_advice_inputs`
- `close_advice`
- `get_close_advice`
- `close_advice_read`

### 通知

- `preview_notification`
- `notification_perception_read`
- `daily_decision_brief_read`

历史 `monthly_income_report`、`portfolio_capital_bridge` 和 `strategy_replay_analyze` 已移除。Research / Shadow Replay / Strategy Lab 使用 `./om research` 人工 CLI，不注册成一个通用 Tool Gateway 工具。

## 常用调用

### 健康检查

```bash
./om-agent run --tool healthcheck \
  --input-json '{"config_key":"us"}'
```

### Runtime 状态

本地 checkout：

```bash
./om-agent run --tool runtime_status \
  --input-json '{"config_key":"us"}'
```

生产 release：

```bash
./om-agent run --tool runtime_status \
  --input-json '{"config_path":"/var/lib/options-monitor/config.us.json"}'
```

### 查询运行历史

```bash
./om-agent run --tool runtime_runs \
  --input-json '{"limit":10}'

./om-agent run --tool runtime_logs \
  --input-json '{"run_id":"<run-id>","kind":"tool","lines":50}'
```

### 候选解释

```bash
./om-agent run --tool candidate_rank_explain \
  --input-json '{"run_id":"<run-id>","account":"lx","mode":"put","top_n":5}'

./om-agent run --tool candidate_filter_explain \
  --input-json '{"run_id":"<run-id>","account":"lx","symbol":"NVDA"}'
```

两者读取已有 artifact，不重跑扫描。

### 扫描

```bash
./om-agent run --tool scan_opportunities \
  --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":5}'
```

`scan_opportunities` 会读取外部数据并物化本地报告。它不发送通知，但不是 no-write 诊断。

### 现金与持仓

```bash
./om-agent run --tool query_cash_headroom \
  --input-json '{"config_key":"us","account":"lx"}'

./om-agent run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
```

`query_cash_headroom` 是 pure-read，并以 `write_cache=false` 查询，不持久化本次 cash query。`option_positions_read` 不写账本，但当前时点查询可能从 OpenD 读取报价。

### Option Performance

```bash
./om-agent run --tool option_performance_report \
  --input-json '{"config_key":"us","account":"lx","period":"mtd"}'
```

利润、现金和活动是不同口径。需要跨月查询时，先用 `analysis_catalog` 查看当前 view 与字段：

```bash
./om-agent run --tool analysis_catalog \
  --input-json '{"config_key":"us"}'

./om-agent run --tool analysis_query \
  --input-json '{"config_key":"us","sql":"select month, account, period_total_pnl_net_cny from option_monthly_performance order by month, account"}'
```

`analysis_query` 只接受白名单 view 上的单条 SELECT / CTE；不要从旧报告猜 view 或 column 名。

### Close Advice

生成和物化新报告：

```bash
./om-agent run --tool get_close_advice \
  --input-json '{"config_key":"us"}'
```

拆分诊断：

```bash
./om-agent run --tool prepare_close_advice_inputs \
  --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice \
  --input-json '{"config_key":"us"}'
```

只读取已有报告：

```bash
./om-agent run --tool close_advice_read \
  --input-json '{"config_key":"us","query":{"option_type":"call","side":"long"}}'
```

前三个入口可能写本地 input/report/cache；`close_advice_read` 不生成新建议。

### Daily Brief

```bash
./om-agent run --tool daily_decision_brief_read \
  --input-json '{"account":"lx","market":"US"}'
```

该工具读取最近一次可靠成功快照，不扫描、不发送、不确认 delivery。

### Symbol 写入

只读 list：

```bash
./om-agent run --tool manage_symbols \
  --input-json '{"config_key":"us","action":"list"}'
```

编辑请求先看 manifest 中的 schema、write predicate、env gate 与 confirm 要求。不要把 `OM_AGENT_ENABLE_WRITE_TOOLS=true` 当成自动授权；它只打开执行门，仍需精确目标和工具级确认语义。

## JSON envelope

调用：

```bash
./om-agent run --tool <tool-name> --input-json '<json>'
```

成功或失败都返回结构化 envelope，常见字段包括：

```json
{
  "schema_version": "1.0",
  "tool_name": "runtime_status",
  "ok": true,
  "data": {},
  "warnings": [],
  "error": null,
  "meta": {}
}
```

集成方应：

1. 先判断 `ok`；
2. 保留 `warnings`、freshness 和 evidence；
3. 读取 `error.code` / `error.details`，不要解析 traceback 文本；
4. 不把 missing / partial / stale 转成零或成功；
5. 不根据自然语言 description 猜 schema。

完整 envelope 与 launcher 合同见 [Agent Integration](AGENT_INTEGRATION.md)。

## 权限与副作用

Tool Gateway 的写门禁针对“实际请求产品/配置写入”的非只读工具。以下两类需要区分：

1. 产品或配置写入：例如 symbol edit、VERSION apply，需要匹配工具的 env gate、dry-run/confirm 和精确目标。
2. 本地物化：例如 scan、portfolio input preparation、Close Advice report，可能写 report/cache，但不会因为 `read_only=true` 自动经过统一 write-tool env gate。

自动化调用前应检查 manifest 的 `side_effects`，并为允许的输出目录设置明确 runtime root。未知工具名会返回结构化错误，不要 fallback 到任意 shell 或内部 Python 模块。

## 与 Copilot / Control 的关系

Tool Gateway 与消息入口是不同能力面：

- Tool Gateway：外部调用方选择一个公开工具；
- Copilot：Host 投影允许的只读工具，回答自由问题；
- Control：显式命令、pending operation、人工确认和审计。

Copilot 不能因为 Tool Gateway 注册了某个写工具就直接写入。详细边界见：

- [OM Capability Surfaces](OM_AGENT_CAPABILITY_MAP.md)
- [Inbound Control](INBOUND_CONTROL.md)
- [OM Copilot v2 Design](OM_COPILOT_V2_DESIGN.md)
