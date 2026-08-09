# S6 Implementation — Tick 编排挂点与 brief 装配

- Slice: S6
- Date: 2026-08-09
- Plan: `docs/gateflow/ai-decision-advice/plan-20260809.md` §S6 + §S6 前置契约

## Files

- `src/application/ai_decision_advice/orchestration.py`（新增）
  - `run_or_reuse_ai_decision_advice`：加载 run state 三类工件
    （candidate snapshot / portfolio context / option positions context）、
    冻结证据索引、构建 frozen inputs，调用 S5 `run_decision_advice`；
  - 未启用 → `not_applicable`；候选快照缺失 → `unavailable:
    candidate_snapshot_missing`；模型未配置 → `unavailable:
    provider_not_configured`；模型异常 → `unavailable`，全程不抛出、不
    阻断回执；
  - DeepSeek Responses 真实 runner 接线（无 web_search、严格 JSON
    schema），api key 经 `DEEPSEEK_API_KEY` 环境变量；
  - portfolio context 适配：list 形式持仓 → `stocks_by_symbol` 映射。
- `src/application/daily_decision_brief_service.py`（改动）
  - 在 normalize 前调用 advice 编排，把 plan §S6 前置契约的
    `ai_decision_advice` 段写入 brief view。
- `domain/domain/daily_decision_brief.py`（改动）
  - `normalize_daily_decision_brief` 归一化 `ai_decision_advice` 段
    （status / 动作 / zero_candidate / reused / evidence_as_of），旧 brief
    无该段 → `None`；
  - `diff_daily_decision_briefs` 扩展：keep/switch/defer/needs_review 任意
    迁移（含 needs_review→keep）→ material `P1`
    `ai_decision_advice_action_changed`；`unavailable` 出现/消失/原因变化
    不产生 material diff（design 14.1）。

## 挂点时序

brief 装配发生在候选身份计算、diff 与渲染之前（`tick_notification_flow`
820 行 persist、968/981 行渲染均消费装配完成的 brief），满足“简报生成
前运行/复用 Advice”；固定简报与候选提醒共享同一 brief 与同一 30s 账户
预算。复用记录在当轮 run 目录落 `reuse_of_advice_id` 绑定。

## 范围裁剪（与 plan 的偏差）

- plan 把 collector CLI 入口 + systemd timer 归在 S6 尾项；实施时按依赖
  关系保留在 S8（timer 文档同 slice 更新，避免文档先于入口）。S6 不含
  collector 入口改动。

## 测试

- 新增 `tests/test_ai_decision_advice_orchestration.py`（5：未启用 / 快照
  缺失 / 完成流转 + 隐私断言 / 跨 run 复用不调模型 / 模型失败降级）。
- 新增 `tests/test_ai_decision_advice_domain_diff.py`（8：normalize 缺省/
  完整段、动作迁移 material、needs_review→keep material、CC scope 迁移、
  unavailable 迁移与原因变化不 material、相同动作无 diff）。
- `tests/test_daily_decision_brief_service.py` 新增 brief 段断言。
- 回归：`test_ai_decision_advice_*` + `test_daily_decision_brief_service`
  + `test_daily_decision_brief_notification_flow` +
  `test_multi_tick_contract_batch2` + `test_unified_tick_entrypoint`
  → 212 passed。
