允许动作：

- keep：维持 Candidate Engine 的策略排序第 1 候选。要求候选、组合、期权
  持仓和外部证据覆盖均完整。
- switch：改选同一策略允许范围内的另一合格候选。Covered Call 只能在同一
  底层标的的合格合约之间改选，不得跨标的。
- defer：本轮不新增该范围的仓位。当风险适用于全部合格候选时使用。
- needs_review：证据冲突、不完整或无法形成明确取舍，需要人工判断。

约束：keep 的 selected_candidate_id 必须等于 baseline_candidate_id；
defer 与 needs_review 不得填写 selected_candidate_id。
