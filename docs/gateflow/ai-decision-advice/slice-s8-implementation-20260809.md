# S8 Implementation — Agent read surface + collector CLI/timer + docs

- Slice: S8
- Date: 2026-08-09

## 范围

按 plan §S8 交付 AI Decision Advice 的运行入口与读面：

1. `om ai-evidence-collector` CLI（`src/interfaces/cli/ai_evidence_collector.py`）：
   - `--config-key`（可重复，默认 us hk）→ 加载 runtime config → observation set →
     symbol identity snapshot → 逐 symbol cutoffs → `run_evidence_collector`；
   - `--dry-run`：只做规划（observed symbols + cutoffs），不发模型调用；
   - 模块关闭 → `{"status":"skipped"}` 退出码 0；无 `DEEPSEEK_API_KEY` 且非 dry-run →
     `{"status":"failed","reason":"missing_api_key"}` 退出码 1；
   - 任何异常收敛为 JSON `failed` 输出 + 退出码 1，不崩溃 timer。
2. systemd collector unit（`src/application/service_deploy.py`）：
   - `render_service_bundle` 在 authoring/runtime config 含
     `ai_decision_advice.enabled: true` 时额外渲染
     `options-monitor-ai-evidence-collector.service` / `.timer`
     （`OnBootSec=2min` + `OnUnitActiveSec=4h` + `Persistent=true`）；
   - authoring config 不完整（AgentToolError）时降级到 JSON runtime config 判定，
     不让可选 add-on 阻断整个 bundle render；
   - 未开启时不渲染任何 collector unit。
3. Agent 读面：`daily_decision_brief_read` 直传 normalized brief 的
   `ai_decision_advice` 区块，新增透传断言。
4. 文档：设计文档实施状态、`docs/AGENT_WIKI.md`（模块、artifact 路径、配置、
   systemd）、`docs/DEPLOY_LINUX_MAC.md`（collector timer 说明）。

## 修复的问题

| # | 问题 | 处置 |
|---|------|------|
| 1 | `run_collector` 的 cutoffs 只覆盖有历史成功的 symbol，新 symbol 无首次搜索 cutoff | `compute_cutoffs` 输入改为逐 observed symbol 映射 |
| 2 | `_ai_decision_advice_enabled_from_authoring_config` 对缺失/不完整的 config.yaml 抛错，使既有 bundle render 回归（8 个既有测试失败） | 文件不存在走 JSON runtime config；AgentToolError 降级到 JSON runtime config |
| 3 | 设计文档冗余字段：7.2“关键集中度”与 symbol/currency 权重重复，§18 残留 industry | 删除“关键集中度”，§18 改为 symbol/currency（v1 无行业维度，代码本就无实现） |

## 验证

- `tests/test_ai_decision_advice_collector_cli.py`（新：4 用例）
- `tests/test_service_deploy.py::test_render_systemd_bundle_ai_evidence_collector_opt_in`（新）
- `tests/test_daily_decision_brief_agent_tool.py::test_read_view_passes_ai_decision_advice_section_through`（新）
- `tests/test_service_deploy.py` 全量 147 passed
- `tests/test_agent_plugin_contract.py` passed
- dependency graph 重新生成，`tests/test_dependency_graph*` passed
