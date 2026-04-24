# PR Material — Productize Local Plugin Workflow And WebUI Phase 1/2

Use this file as the source material for the GitHub PR body for the current productization batch.

## PR Title

`feat: productize local plugin workflow and webui phase 1/2`

## PR Summary

收敛本地插件安装/初始化、账户与通知配置、WebUI 结果与历史页，以及 Agent 输出结构，降低新用户接入和 UI/Agent 消费成本。

## PR Body

继续把仓库从“个人脚本集合”收敛成“可独立安装、本地运行、可被 WebUI 和 Agent 稳定消费的本地插件产品”。

- 明确本地插件入口与初始化路径，继续去除私有路径和私有工作区假设
- 收敛最小配置与示例配置，面向独立安装用户
- WebUI 支持新增、编辑、删除 `futu` / `external_holdings` 账户
- 展示账户主路径与兜底路径状态，明确 `futu` 主路径和 `holdings` fallback 的可用性
- WebUI 支持通知开关、target、quiet hours、现金 footer，以及 preview / check / dry-run / test-send
- WebUI 可直接触发 `healthcheck`、`scan_opportunities`、`get_close_advice`
- WebUI 可查看结构化结果、产物快照、最近工具执行、审计事件、last run 和 tick metrics
- 工具失败时补充用户可执行的修复建议
- 公开 spec 增加 `requires`、`capabilities`、`side_effects`、`recommended_flow`
- `scan_opportunities` 新增 `data.summary`、`data.top_candidates`
- `close_advice` 新增 `data.summary`、`data.top_rows`、`data.notification_preview`
- `get_close_advice` 新增 `data.summary`、`data.top_rows`、`data.notification_preview`

## Validation

```bash
python3 -m pytest tests/test_webui_symbol_strategy_cleanup.py tests/test_agent_plugin_contract.py tests/test_account_config.py tests/test_runtime_config_sync.py tests/test_agent_plugin_smoke.py
python3 -m pytest tests/test_agent_plugin_smoke.py tests/test_agent_plugin_contract.py tests/test_webui_symbol_strategy_cleanup.py
python3 tests/run_smoke.py
bash scripts/webui/build_frontend.sh
```

## Checklist

- [x] runtime config / 最小配置可读
- [x] 账户新增编辑删除可用
- [x] 通知 preview/check/dry-run/test-send 可用
- [x] `healthcheck` / `scan_opportunities` / `get_close_advice` 可从 WebUI 触发
- [x] 历史与审计结果可查看
- [x] Agent 返回结构增加 `summary` / `top_*` 字段

## Risk / Follow-ups

- 当前仍有较多现存未提交改动；执行提交时需要确认是否全部纳入同一主题 PR
- `get_close_advice` 和结果页虽然已收敛，但 UI 仍偏 JSON 视图，不是最终产品态卡片页
- 版本发布、升级迁移、Docker、本地 HTTP API 仍未完成
