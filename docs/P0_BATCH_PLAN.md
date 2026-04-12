# P0 Batch 架构改造清单（先拆决策，再固化边界，再收口入口）

## Batch-1（本次）: 先拆决策引擎雏形
- 目标:
  - 在不改变业务结果语义前提下，抽离多账户 tick 的业务决策入口（过滤/排序/降级判定）。
  - `scripts/multi_tick/main.py` 改为调用 `om/domain/engine`，减少内联判断。
- 涉及文件:
  - `om/domain/engine/__init__.py`（新增）
  - `om/domain/engine/decision_engine.py`（新增）
  - `scripts/multi_tick/main.py`（改调用入口）
  - `tests/test_domain_engine_batch1.py`（新增最小覆盖）
- 验收标准:
  - 新增引擎入口函数:
    - `decide_opend_degrade_to_yahoo`
    - `filter_notify_candidates`
    - `rank_notify_candidates`
  - `main.py` 中 OpenD 降级判定改为调用引擎入口，不再内联 `allow_downgrade && !has_hk_opend && !watchdog_timed_out`。
  - `main.py` 中通知候选选择改为调用引擎入口（过滤+排序入口）。
  - 现有语义不变（单测通过）。
- 回滚点:
  - 回滚 commit 后，`main.py` 恢复为原有内联判断；`om/domain/engine` 与对应测试可整体移除。

## Batch-2（后续）: 固化边界（I/O 与决策分离）
- 目标:
  - 将 `main.py` 中“读配置/调用工具/写状态”与“业务决策”分层，形成稳定输入输出 DTO。
- 计划涉及文件:
  - `om/domain/engine/*`（扩展）
  - `scripts/multi_tick/main.py`
  - `scripts/infra/service.py`（仅边界适配）
  - `om/storage/repositories/*`（必要时）
- 验收标准:
  - 决策层函数不直接依赖 subprocess/文件系统。
  - I/O 失败与业务决策失败在审计字段可区分。
- 回滚点:
  - 保持 `main.py` 单文件 orchestrator 入口，决策调用退回现有 domain helper。

## Batch-3（后续）: 收口入口（单一编排入口）
- 目标:
  - 合并多处决策调用路径，形成单一编排入口（主流程仅保留 orchestration）。
- 计划涉及文件:
  - `scripts/multi_tick/main.py`
  - `om/domain/engine/*`
  - `tests/test_multi_tick_*`（补回归）
- 验收标准:
  - 入口函数职责明确：编排、依赖注入、审计；业务规则只在 engine。
  - 回归测试覆盖关键路径（scheduler / watchdog / notify）。
- 回滚点:
  - 入口收口前的 tag/commit，可按批次回退。
