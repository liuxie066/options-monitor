# Docs Index

本文只索引当前维护的 living docs，并说明历史证据的边界。

遇到冲突时，权威顺序是：

1. 当前源码、配置验证器和测试；
2. 当前 runtime artifact / SQLite / provider evidence；
3. 本索引中的 living docs；
4. migration note、Gateflow、plan 和 review 历史。

## 开始使用

- [README](../README.md)：产品定位、五分钟开始、常用入口和安全边界。
- [Install](INSTALL.md)：Python 要求、安装器、release 目录和 wrapper。
- [Getting Started](GETTING_STARTED.md)：安装后的首次配置、检查和首跑。
- [Deploy](../DEPLOY.md)：部署入口和目录契约。
- [Linux / Mac Deployment](DEPLOY_LINUX_MAC.md)：systemd / launchd 详细部署。
- [Runbook](../RUNBOOK.md)：巡检、调度、故障诊断和应急操作。

## 配置

- [Config Contract](../CONFIGS.md)：`config.yaml -> build -> runtime JSON` 权威链和迁移边界。
- [Configuration Guide](../CONFIGURATION_GUIDE.md)：账户、市场、环境变量、通知和验证方法。
- [Security](../SECURITY.md)：漏洞和敏感信息处理。

配置字段会持续演进。静态文档只记录稳定心智模型；具体字段来源优先使用：

```bash
./om config explain --source yaml --market us --key <dot.path>
./om config validate --source yaml --market us
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

## 产品与策略

- [Product Architecture](PRODUCT_ARCHITECTURE.md)：产品域、模块责任和依赖。
- [Strategy Architecture](STRATEGY_ARCHITECTURE.md)：Sell Put、Covered Call、Combo Yield 的开仓边界。
- [Candidate Strategy](candidate_strategy.md)：当前候选筛选、排序和 trace。
- [Opportunity Quality](OPPORTUNITY_QUALITY.md)：Shadow Replay 和扫描质量判定口径。
- [Notification Experience PRD](OPTION_NOTIFICATION_EXPERIENCE_PRD.md)：scheduled report、增量提醒和主动查询体验。
- [Strategy Lab Design](STRATEGY_LAB_DESIGN.md)：策略实验、证据要求和生产边界。

## 技术架构与核心合同

- [Architecture](ARCHITECTURE.md)：技术分层、入口和真实调用链。
- [Ledger Architecture](LEDGER_ARCHITECTURE.md)：`trade_events -> position_lots`、lot identity 和恢复流程。
- [Close Advice Contract](CLOSE_ADVICE_CONTRACT.md)：exit state、报价证据和组合动作。
- [Position Advice v2 Contract](POSITION_ADVICE_V2_CONTRACT.md)：portfolio advice、source receipt、allocator、reader、authority 与 promotion 合同。
- [Position Advice Compatibility](POSITION_ADVICE_COMPATIBILITY.md)：v1/v2 mixed-version、rollout、rollback 与通知兼容矩阵。
- [Option Performance Design](OPTION_PERFORMANCE_DESIGN.md)：period、PnL、cash、activity 和 bridge 合同。
- [Assigned Stock Return Design](ASSIGNED_STOCK_RETURN_DESIGN.md)：assignment 后的正股事实和收益归因。
- [OM Runtime and Data Quality](quality-monitoring/README.md)：OM 本地检查实现与操作入口；跨系统正式设计由 `investment-quality` 维护。
- [Dependency Graph](DEPENDENCY_GRAPH.md)：由生成脚本维护的 Python import graph。

## Tool Gateway、Copilot 与消息入口

- [Agent Getting Started](AGENT_GETTING_STARTED.md)：最短 Tool Gateway 接入。
- [Agent Integration](AGENT_INTEGRATION.md)：JSON envelope、manifest 和权限合同。
- [Tool Reference](TOOL_REFERENCE.md)：公开工具分类、风险 metadata 和常用示例。
- [OM Capability Surfaces](OM_AGENT_CAPABILITY_MAP.md)：Tool Gateway、Control、Copilot 的能力边界。
- [Inbound Control](INBOUND_CONTROL.md)：确定性 Control、pending operation 和 channel 安全。
- [OM Copilot v2 Design](OM_COPILOT_V2_DESIGN.md)：当前自由问答 Copilot 架构。
- [Agent Handbook](AGENT_WIKI.md)：本地 agent 的任务 playbook、模块地图和验证矩阵。
- [Session Summary](SESSION_SUMMARY.md)：仅在显式 handoff 时使用的模板。

`./om-agent spec` 是公开工具清单的运行时权威。Tool Reference 不复制每个工具的完整 schema。

## 运维、修复与发布

- [Guardrails](GUARDRAILS.md)：本地 hook 与 CI 门禁。
- [Option Positions Repair](OPTION_POSITIONS_REPAIR.md)：账本错账的只读诊断、dry-run 和修复。
- [Shadow Replay Runbook](SHADOW_REPLAY_RUNBOOK.md)：dataset、mark、settlement 和 review readiness。
- [Release Process](RELEASE_PROCESS.md)：VERSION 驱动的发布流程。

## 迁移与历史兼容

- [Option Performance v1 Migration](migrations/OPTION_PERFORMANCE_V1_MIGRATION.md)：旧 monthly-income 输出如何映射到当前 Performance；不是 rollback path。
- [Trade And Position Ledger Redesign](TRADE_POSITION_LEDGER_REDESIGN.md)：已完成重构的兼容指针；当前合同见 Ledger Architecture。

历史兼容文档只能解释旧 artifact，不能覆盖当前代码行为。

## 工作流证据

以下目录保存阶段性过程证据，不属于 living docs：

- `docs/gateflow/`
- `docs/reviews/`
- `docs/plans/`

它们可能记录特定 commit、PR、当时的发现、实施切片和已关闭计划。不要逐份更新成“当前状态”；完成的 work unit 应以 final closeout / Git 历史追溯。新功能规范应进入上面的产品或技术合同，而不是继续堆在 review artifact 中。
