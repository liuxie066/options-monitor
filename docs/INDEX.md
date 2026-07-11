# Docs Index

按用途看文档：

## 产品使用
- [../README.md](../README.md)：产品介绍、安装、初始化、常用命令
- [INSTALL.md](INSTALL.md)：安装方式、release 目录布局和 installer 安全契约
- [GETTING_STARTED.md](GETTING_STARTED.md)：普通用户首次运行路径

## 配置
- [../CONFIGS.md](../CONFIGS.md)：配置来源与 canonical config 约定
- [../CONFIGURATION_GUIDE.md](../CONFIGURATION_GUIDE.md)：详细配置字段说明

## Tool Gateway / Inbound Assistant
- [../AGENTS.md](../AGENTS.md)：本地 agent 首屏说明书、安全红线、入口层级、模块归属（静态前缀，Prompt Cache 友好）
- [../CLAUDE.md](../CLAUDE.md)：Claude 特有补充指令
- [ARCHITECTURE.md](ARCHITECTURE.md)：当前系统架构和入口边界；说明 CLI、Tool Gateway、Inbound Assistant 的职责
- [INBOUND_CONTROL.md](INBOUND_CONTROL.md)：`./om assistant handle` 远程消息入口的安全控制层
- [OM_COPILOT_V2_DESIGN.md](OM_COPILOT_V2_DESIGN.md)：当前自由问答 Copilot v2 的架构、运行合同和交付门槛
- [OM_ASSISTANT_MEMORY_DESIGN.md](OM_ASSISTANT_MEMORY_DESIGN.md)：人工接受的长期偏好记忆与 Copilot 会话记忆边界
- [AGENT_INTEGRATION.md](AGENT_INTEGRATION.md)：`./om-agent` Tool Gateway JSON 集成合同
- [AGENT_GETTING_STARTED.md](AGENT_GETTING_STARTED.md)：本地 Tool Gateway 工具快速开始
- [AGENT_WIKI.md](AGENT_WIKI.md)：本地 agent 任务手册（工具选择、Research / Shadow Replay 侧线、排障 playbook、模块地图、验证矩阵）
- [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md)：Tool Gateway 与 Inbound Assistant 的能力边界、LLM 暴露面和验证方式
- [TOOL_REFERENCE.md](TOOL_REFERENCE.md)：工具参考
- [SESSION_SUMMARY.md](SESSION_SUMMARY.md)：显式 handoff 时使用的摘要模板

### 历史设计参考

以下文档保留为历史设计和阶段性路线图，不作为当前架构或术语权威。当前命名和边界以
[ARCHITECTURE.md](ARCHITECTURE.md) 和 [INBOUND_CONTROL.md](INBOUND_CONTROL.md) 为准。

- `OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md` / `OM_ASSISTANT_TOOL_LOOP_COMPLETION_PLAN.md` 已删除；旧 provider tool-calling loop 不再是当前架构或迁移目标
- `OM_AGENT_COMPLETION_DESIGN.md` / `OM_AGENT_INTELLIGENCE_UPGRADE_PLAN.md` / `AGENT_RELIABILITY_P0_P2_DESIGN.md` / `SQLITE_TOOL_OS_EXPANSION_DESIGN.md` 已删除；旧自由问答路线图不再是当前架构或迁移目标
- `OM_ASSISTANT_CONVERSATION_CONTEXT_DESIGN.md` / `OM_ASSISTANT_CONTEXT_EVAL_PLAN.md` / `OM_ASSISTANT_CONTEXT_IMPLEMENTATION_PLAN.md` / `OM_ASSISTANT_CONTEXT_PROJECTION_CONTRACT.md` / `OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md` 已删除；旧上下文 projection/validation 不再作为当前任务系统基础

## 运行与运维
- [../DEPLOY.md](../DEPLOY.md)：Linux / Mac 服务化部署入口
- [DEPLOY_LINUX_MAC.md](DEPLOY_LINUX_MAC.md)：systemd / launchd 部署、runtime_root 和 store 检查
- [../RUNBOOK.md](../RUNBOOK.md)：运维巡检、cron、应急处理
- [OPTION_POSITIONS_REPAIR.md](OPTION_POSITIONS_REPAIR.md)：`option_positions` / `position_lots` 错账修复手册
- [RELEASE_PROCESS.md](RELEASE_PROCESS.md)：维护者发布清单

## 业务规则
- [PRODUCT_ARCHITECTURE.md](PRODUCT_ARCHITECTURE.md)：产品域、模块定义、模块依赖和当前实现差距
- [STRATEGY_ARCHITECTURE.md](STRATEGY_ARCHITECTURE.md)：开仓策略架构、Sell Put / Covered Call / Combo Yield 策略边界
- [ASSIGNED_STOCK_RETURN_DESIGN.md](ASSIGNED_STOCK_RETURN_DESIGN.md)：Sell Put 被指派后正股成本、实时 spot、历史数据和收益统计口径设计
- [STRATEGY_LAB_DESIGN.md](STRATEGY_LAB_DESIGN.md)：Strategy Lab 策略进化实验室的 PRD、架构、技术方案和安全边界
- [candidate_strategy.md](candidate_strategy.md)：候选筛选与排序规则
- [SHADOW_REPLAY_RUNBOOK.md](SHADOW_REPLAY_RUNBOOK.md)：Research / Shadow Replay 独立离线模块的数据采样、OpenD 补价、review readiness 和 candidate-impact 对比操作手册
- [CLOSE_ADVICE_CONTRACT.md](CLOSE_ADVICE_CONTRACT.md)：平仓建议 exit-state、收益增强组合和渲染契约

## 安全与约束
- [GUARDRAILS.md](GUARDRAILS.md)：guardrails 与安全门禁

## 架构
- [ARCHITECTURE.md](ARCHITECTURE.md)：系统分层、入口点、运行时流程（面向人类开发者）
- [TRADE_POSITION_LEDGER_REDESIGN.md](TRADE_POSITION_LEDGER_REDESIGN.md)：交易与持仓账本架构、风控边界和验收标准
