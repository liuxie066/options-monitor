# Trade And Position Ledger Redesign

> Historical compatibility pointer.

这份重构计划已经完成，不再作为当前架构或迁移路线图维护。当前账本事实链、模块所有权、写入语义和恢复流程以 [Ledger Architecture](LEDGER_ARCHITECTURE.md) 为准。

旧 v1/v2 混合模型、Feishu `option_positions` 镜像和 compatibility facade 已退出稳态运行。需要追溯迁移阶段、旧模块名或历史验收证据时，请查看 Git 历史；不要从旧设计阶段推导当前运行行为。
